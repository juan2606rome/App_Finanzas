// ruta: microservicio-java/Main.java
// Microservicio en Java (JDK 17+, sin Maven ni librerías externas).
// Hace lo mismo que los de Python, Node.js y Go:
//   GET    /api/tasa              -> tasas de cambio (todas las monedas)
//   POST   /api/preguntar         -> IA (Mistral) con contexto real de Supabase
//   POST   /api/cuentas           -> INSERTAR una cuenta
//   PUT    /api/cuentas/{id}      -> ACTUALIZAR una cuenta
//   DELETE /api/cuentas/{id}      -> ELIMINAR una cuenta
//   POST   /api/transacciones     -> INSERTAR una transacción (historial)

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

public class Main {

    // -----------------------------------------------------------------
    // Configuración (variables de entorno, igual que en Render)
    // -----------------------------------------------------------------
    static String env(String clave, String defecto) {
        String v = System.getenv(clave);
        return (v == null || v.trim().isEmpty()) ? defecto : v.trim();
    }

    // Acepta la URL con o sin "/rest/v1" al final (para evitar la URL duplicada).
    static String normalizarUrl(String url) {
        String u = url.trim().replaceAll("/+$", "");
        u = u.replaceAll("(?i)/rest/v1$", "");
        return u.replaceAll("/+$", "");
    }

    static final String SUPABASE_URL = normalizarUrl(env("SUPABASE_URL", ""));
    static final String SUPABASE_KEY = env("SUPABASE_KEY", "");
    static final String MISTRAL_API_KEY = env("MISTRAL_API_KEY", "");
    static final String MISTRAL_URL = env("MISTRAL_URL", "https://api.mistral.ai/v1/chat/completions");
    static final int MAX_MOVIMIENTOS_CONTEXTO = 15;

    static final List<String> CAMPOS_CUENTA = List.of("id", "nombre", "saldo", "fecha_creacion", "actualizado_en");
    static final List<String> CAMPOS_TRANSACCION = List.of(
            "id", "tipo", "cuenta_origen_id", "cuenta_destino_id",
            "cuenta_origen_nombre", "cuenta_destino_nombre", "monto", "descripcion", "fecha");
    // merge-duplicates: si el id ya existe, la fila se actualiza en vez de fallar.
    static final String UPSERT = "resolution=merge-duplicates,return=minimal";
    static final Pattern SOLO_DIGITOS = Pattern.compile("^\\d+$");

    static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    // -----------------------------------------------------------------
    // JSON mínimo (para no depender de librerías externas).
    // Los objetos son Map, los arreglos List, los enteros Long, los
    // decimales Double, y además String, Boolean y null.
    // -----------------------------------------------------------------
    static final class Json {
        private final String s;
        private int i = 0;

        private Json(String s) { this.s = s; }

        static Object parse(String texto) {
            try {
                Json p = new Json(texto);
                p.ws();
                Object v = p.valor();
                p.ws();
                if (p.i != p.s.length()) throw new IllegalArgumentException("JSON inválido");
                return v;
            } catch (IndexOutOfBoundsException | NumberFormatException e) {
                throw new IllegalArgumentException("JSON inválido");
            }
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
        }

        private Object valor() {
            char c = s.charAt(i);
            switch (c) {
                case '{': return objeto();
                case '[': return arreglo();
                case '"': return cadena();
                case 't': return literal("true", Boolean.TRUE);
                case 'f': return literal("false", Boolean.FALSE);
                case 'n': return literal("null", null);
                default: return numero();
            }
        }

        private Object literal(String txt, Object v) {
            if (!s.startsWith(txt, i)) throw new IllegalArgumentException("JSON inválido");
            i += txt.length();
            return v;
        }

        private Map<String, Object> objeto() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++;
            ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String k = cadena();
                ws();
                if (s.charAt(i++) != ':') throw new IllegalArgumentException("JSON inválido");
                ws();
                m.put(k, valor());
                ws();
                char c = s.charAt(i++);
                if (c == '}') return m;
                if (c != ',') throw new IllegalArgumentException("JSON inválido");
            }
        }

        private List<Object> arreglo() {
            List<Object> l = new ArrayList<>();
            i++;
            ws();
            if (s.charAt(i) == ']') { i++; return l; }
            while (true) {
                ws();
                l.add(valor());
                ws();
                char c = s.charAt(i++);
                if (c == ']') return l;
                if (c != ',') throw new IllegalArgumentException("JSON inválido");
            }
        }

        private String cadena() {
            if (s.charAt(i) != '"') throw new IllegalArgumentException("JSON inválido");
            i++;
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c != '\\') { sb.append(c); continue; }
                char e = s.charAt(i++);
                switch (e) {
                    case 'n': sb.append('\n'); break;
                    case 't': sb.append('\t'); break;
                    case 'r': sb.append('\r'); break;
                    case 'b': sb.append('\b'); break;
                    case 'f': sb.append('\f'); break;
                    case '/': sb.append('/'); break;
                    case '\\': sb.append('\\'); break;
                    case '"': sb.append('"'); break;
                    case 'u':
                        sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                        break;
                    default: throw new IllegalArgumentException("JSON inválido");
                }
            }
        }

        private Object numero() {
            int ini = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String t = s.substring(ini, i);
            if (t.isEmpty()) throw new IllegalArgumentException("JSON inválido");
            if (SOLO_DIGITOS.matcher(t.startsWith("-") ? t.substring(1) : t).matches()) {
                try { return Long.parseLong(t); } catch (NumberFormatException e) { return Double.parseDouble(t); }
            }
            return Double.parseDouble(t);
        }

        // ---------- Serializar a texto JSON ----------
        static String stringify(Object o) {
            StringBuilder sb = new StringBuilder();
            escribir(sb, o);
            return sb.toString();
        }

        private static void escribir(StringBuilder sb, Object o) {
            if (o == null) {
                sb.append("null");
            } else if (o instanceof String str) {
                comillas(sb, str);
            } else if (o instanceof Boolean || o instanceof Long || o instanceof Integer) {
                sb.append(o);
            } else if (o instanceof Double d) {
                sb.append(d.isNaN() || d.isInfinite() ? "null" : d.toString());
            } else if (o instanceof Map<?, ?> m) {
                sb.append('{');
                boolean primero = true;
                for (Map.Entry<?, ?> e : m.entrySet()) {
                    if (!primero) sb.append(',');
                    primero = false;
                    comillas(sb, String.valueOf(e.getKey()));
                    sb.append(':');
                    escribir(sb, e.getValue());
                }
                sb.append('}');
            } else if (o instanceof List<?> l) {
                sb.append('[');
                for (int k = 0; k < l.size(); k++) {
                    if (k > 0) sb.append(',');
                    escribir(sb, l.get(k));
                }
                sb.append(']');
            } else {
                comillas(sb, o.toString());
            }
        }

        private static void comillas(StringBuilder sb, String str) {
            sb.append('"');
            for (int k = 0; k < str.length(); k++) {
                char c = str.charAt(k);
                switch (c) {
                    case '"': sb.append("\\\""); break;
                    case '\\': sb.append("\\\\"); break;
                    case '\n': sb.append("\\n"); break;
                    case '\r': sb.append("\\r"); break;
                    case '\t': sb.append("\\t"); break;
                    default:
                        if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                        else sb.append(c);
                }
            }
            sb.append('"');
        }
    }

    // -----------------------------------------------------------------
    // Utilidades
    // -----------------------------------------------------------------
    static Map<String, Object> obj(Object... pares) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int k = 0; k < pares.length; k += 2) m.put((String) pares[k], pares[k + 1]);
        return m;
    }

    static void enviar(HttpExchange ex, int codigo, Object cuerpo) throws IOException {
        byte[] bytes = Json.stringify(cuerpo).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(codigo, bytes.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(bytes); }
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> leerJson(HttpExchange ex) throws IOException {
        String texto = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8).trim();
        if (texto.isEmpty()) return new LinkedHashMap<>();
        Object v = Json.parse(texto);
        if (!(v instanceof Map)) throw new IllegalArgumentException("JSON inválido");
        return (Map<String, Object>) v;
    }

    static Map<String, Object> filtrar(Map<String, Object> origen, List<String> campos) {
        Map<String, Object> salida = new LinkedHashMap<>();
        for (String c : campos) if (origen.containsKey(c)) salida.put(c, origen.get(c));
        return salida;
    }

    static String texto(Object v) { return v == null ? "" : String.valueOf(v); }

    static boolean esId(Object v) { return SOLO_DIGITOS.matcher(texto(v)).matches(); }

    static Double numero(Object v) {
        if (v == null) return null;
        try { return Double.parseDouble(String.valueOf(v)); } catch (NumberFormatException e) { return null; }
    }

    static boolean supabaseConfigurado() { return !SUPABASE_URL.isEmpty() && !SUPABASE_KEY.isEmpty(); }

    record Respuesta(int status, String cuerpo) {
        boolean ok() { return status >= 200 && status < 300; }
    }

    static Respuesta supabase(String metodo, String tabla, String query, String cuerpoJson, String prefer)
            throws IOException, InterruptedException {
        String url = SUPABASE_URL + "/rest/v1/" + tabla + (query.isEmpty() ? "" : "?" + query);
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(6))
                .header("apikey", SUPABASE_KEY)
                .header("Authorization", "Bearer " + SUPABASE_KEY);
        if (prefer != null) b.header("Prefer", prefer);
        if (cuerpoJson != null) {
            b.header("Content-Type", "application/json");
            b.method(metodo, HttpRequest.BodyPublishers.ofString(cuerpoJson));
        } else {
            b.method(metodo, HttpRequest.BodyPublishers.noBody());
        }
        HttpResponse<String> r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
        return new Respuesta(r.statusCode(), r.body());
    }

    // Devuelve la lista de filas, o null si algo falla.
    @SuppressWarnings("unchecked")
    static List<Map<String, Object>> consultar(String tabla, String query) {
        try {
            Respuesta r = supabase("GET", tabla, query, null, null);
            if (!r.ok()) return null;
            return (List<Map<String, Object>>) (List<?>) Json.parse(r.cuerpo());
        } catch (Exception e) {
            return null;
        }
    }

    // Ejecuta una escritura en Supabase y responde con el resultado.
    static void escribir(HttpExchange ex, String metodo, String tabla, String query, Object cuerpo,
                         String prefer, int codigoOk, Object respuestaOk) throws IOException {
        if (!supabaseConfigurado()) {
            enviar(ex, 500, obj("error", "falta configurar SUPABASE_URL y SUPABASE_KEY."));
            return;
        }
        Respuesta r;
        try {
            r = supabase(metodo, tabla, query, cuerpo == null ? null : Json.stringify(cuerpo), prefer);
        } catch (Exception e) {
            enviar(ex, 502, obj("error", "No se pudo contactar a Supabase: " + e.getMessage()));
            return;
        }
        if (!r.ok()) {
            String detalle = r.cuerpo().length() > 300 ? r.cuerpo().substring(0, 300) : r.cuerpo();
            enviar(ex, 502, obj("error", "Supabase respondió " + r.status() + ": " + detalle));
            return;
        }
        enviar(ex, codigoOk, respuestaOk);
    }

    static String formatearPesos(Object v) {
        Double n = numero(v);
        if (n == null) return texto(v);
        return String.format(Locale.US, "%,d", Math.round(n)).replace(',', '.');
    }

    static String formatearFecha(Object v) {
        try {
            return OffsetDateTime.parse(texto(v)).format(DateTimeFormatter.ofPattern("dd/MM/yyyy HH:mm"));
        } catch (Exception e) {
            return texto(v);
        }
    }

    static double sumaSaldos(List<Map<String, Object>> cuentas) {
        double total = 0;
        if (cuentas != null) {
            for (Map<String, Object> c : cuentas) {
                Double n = numero(c.get("saldo"));
                if (n != null) total += n;
            }
        }
        return total;
    }

    // -----------------------------------------------------------------
    // LECTURA: tasas
    // -----------------------------------------------------------------
    static void obtenerTasa(HttpExchange ex) throws IOException {
        if (!supabaseConfigurado()) {
            enviar(ex, 500, obj("error", "falta configurar SUPABASE_URL y SUPABASE_KEY."));
            return;
        }
        List<Map<String, Object>> filas = consultar("tasas", "select=*&order=moneda.asc");
        if (filas == null) {
            enviar(ex, 502, obj("error", "No se pudo consultar Supabase."));
            return;
        }
        if (filas.isEmpty()) {
            enviar(ex, 404, obj("error", "La tabla 'tasas' está vacía en Supabase."));
            return;
        }
        List<Object> tasas = new ArrayList<>();
        for (Map<String, Object> f : filas) {
            if (texto(f.get("moneda")).isEmpty() || f.get("tasa_cop") == null) continue;
            tasas.add(obj("moneda", f.get("moneda"), "tasa_cop", f.get("tasa_cop"),
                    "fecha_actualizacion", f.get("fecha_actualizacion")));
        }
        enviar(ex, 200, obj("tasas", tasas));
    }

    // -----------------------------------------------------------------
    // LECTURA: IA con contexto financiero
    // -----------------------------------------------------------------
    static String construirContexto() {
        if (!supabaseConfigurado()) return "(No hay conexión configurada a la base de datos del usuario.)";

        List<Map<String, Object>> cuentas = consultar("cuentas", "select=nombre,saldo,actualizado_en&order=nombre.asc");
        List<Map<String, Object>> tasas = consultar("tasas", "select=*&order=moneda.asc");
        List<Map<String, Object>> movs = consultar("transacciones",
                "select=*&order=fecha.desc&limit=" + MAX_MOVIMIENTOS_CONTEXTO);

        List<String> partes = new ArrayList<>();

        if (cuentas == null) {
            partes.add("No se pudieron leer las cuentas del usuario en este momento.");
        } else if (cuentas.isEmpty()) {
            partes.add("El usuario todavía no tiene ninguna cuenta creada.");
        } else {
            List<String> lineas = new ArrayList<>();
            for (Map<String, Object> c : cuentas) {
                lineas.add("- " + texto(c.get("nombre")) + ": $" + formatearPesos(c.get("saldo")) + " COP");
            }
            partes.add("Cuentas actuales del usuario (en pesos colombianos, COP):\n" + String.join("\n", lineas)
                    + "\nTotal en todas las cuentas: $" + formatearPesos(sumaSaldos(cuentas)) + " COP");
        }

        if (tasas != null && !tasas.isEmpty()) {
            double total = sumaSaldos(cuentas);
            List<String> lineas = new ArrayList<>();
            for (Map<String, Object> t : tasas) {
                Double tasa = numero(t.get("tasa_cop"));
                String moneda = texto(t.get("moneda"));
                if (tasa == null || tasa == 0 || moneda.isEmpty()) continue;
                double equivalente = Math.round(total / tasa * 100.0) / 100.0;
                lineas.add("- 1 " + moneda + " = $" + formatearPesos(tasa) + " COP (el total del usuario equivale a ~"
                        + equivalente + " " + moneda + ")");
            }
            if (!lineas.isEmpty()) partes.add("Tasas de cambio actuales:\n" + String.join("\n", lineas));
        }

        if (movs == null) {
            partes.add("No se pudo leer el historial de movimientos en este momento.");
        } else if (movs.isEmpty()) {
            partes.add("El usuario todavía no tiene movimientos registrados.");
        } else {
            List<String> lineas = new ArrayList<>();
            for (Map<String, Object> m : movs) {
                String monto = formatearPesos(m.get("monto"));
                String origen = texto(m.get("cuenta_origen_nombre"));
                if (origen.isEmpty()) origen = "(cuenta eliminada)";
                String destino = texto(m.get("cuenta_destino_nombre"));
                if (destino.isEmpty()) destino = "(cuenta eliminada)";
                String tipo = texto(m.get("tipo"));
                String detalle;
                switch (tipo) {
                    case "transferencia": detalle = "transferencia de $" + monto + " de " + origen + " a " + destino; break;
                    case "deposito": detalle = "depósito de $" + monto + " en " + destino; break;
                    case "retiro": detalle = "retiro de $" + monto + " de " + origen; break;
                    default: detalle = tipo + " de $" + monto;
                }
                String desc = texto(m.get("descripcion")).trim();
                if (!desc.isEmpty()) detalle += " (descripción: " + desc + ")";
                lineas.add("- " + formatearFecha(m.get("fecha")) + ": " + detalle);
            }
            partes.add("Últimos " + lineas.size() + " movimientos del usuario (el primero es el más reciente):\n"
                    + String.join("\n", lineas));
        }

        return String.join("\n\n", partes);
    }

    static final String PROMPT_SISTEMA = "Eres el asistente de una app de finanzas personales que "
            + "usa pesos colombianos (COP). A continuación tienes la "
            + "información real y actualizada de las cuentas, tasas de "
            + "cambio y movimientos del usuario. Úsala para responder "
            + "sus preguntas con datos exactos (por ejemplo, cuánto "
            + "dinero tiene en total, a cuántos dólares equivale, o "
            + "cuáles fueron sus últimos movimientos). Si la pregunta "
            + "no tiene que ver con esta información, respóndela igual "
            + "con tus conocimientos generales. Si no encuentras el "
            + "dato exacto que te piden en la información de abajo, "
            + "dilo claramente en vez de inventarlo. Responde en "
            + "español, corto y claro.\n\n"
            + "=== INFORMACIÓN ACTUAL DEL USUARIO ===\n";

    static void preguntarIA(HttpExchange ex) throws IOException {
        if (MISTRAL_API_KEY.isEmpty()) {
            enviar(ex, 500, obj("error", "Falta configurar MISTRAL_API_KEY."));
            return;
        }
        Map<String, Object> datos = leerJson(ex);
        String pregunta = texto(datos.get("pregunta")).trim();
        if (pregunta.isEmpty()) {
            enviar(ex, 400, obj("error", "No se recibió ninguna pregunta."));
            return;
        }

        String contexto = construirContexto();

        List<Object> mensajes = List.of(
                obj("role", "system", "content", PROMPT_SISTEMA + contexto),
                obj("role", "user", "content", pregunta));
        String cuerpo = Json.stringify(obj("model", "ministral-8b-2512", "messages", mensajes, "temperature", 0.3));

        try {
            HttpRequest req = HttpRequest.newBuilder(URI.create(MISTRAL_URL))
                    .timeout(Duration.ofSeconds(15))
                    .header("Authorization", "Bearer " + MISTRAL_API_KEY)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(cuerpo))
                    .build();
            HttpResponse<String> r = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
            if (r.statusCode() < 200 || r.statusCode() >= 300) {
                enviar(ex, 502, obj("error", "No se pudo contactar a Mistral: HTTP " + r.statusCode()));
                return;
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> json = (Map<String, Object>) Json.parse(r.body());
            @SuppressWarnings("unchecked")
            List<Object> choices = (List<Object>) json.get("choices");
            @SuppressWarnings("unchecked")
            Map<String, Object> mensaje = (Map<String, Object>) ((Map<String, Object>) choices.get(0)).get("message");
            enviar(ex, 200, obj("respuesta", texto(mensaje.get("content"))));
        } catch (Exception e) {
            enviar(ex, 502, obj("error", "No se pudo contactar a Mistral: " + e.getMessage()));
        }
    }

    // -----------------------------------------------------------------
    // ESCRITURA: insertar / actualizar / eliminar
    // -----------------------------------------------------------------
    static boolean nombreValido(Map<String, Object> fila) { return !texto(fila.get("nombre")).trim().isEmpty(); }

    static boolean saldoValido(Map<String, Object> fila) { return numero(fila.get("saldo")) != null; }

    static void insertarCuenta(HttpExchange ex) throws IOException {
        Map<String, Object> fila = filtrar(leerJson(ex), CAMPOS_CUENTA);
        if (!esId(fila.get("id")) || !nombreValido(fila) || !saldoValido(fila)) {
            enviar(ex, 400, obj("error", "Se requiere id, nombre y saldo."));
            return;
        }
        escribir(ex, "POST", "cuentas", "on_conflict=id", fila, UPSERT, 201,
                obj("ok", true, "operacion", "insertar", "id", fila.get("id")));
    }

    static void actualizarCuenta(HttpExchange ex, String idTexto) throws IOException {
        if (!SOLO_DIGITOS.matcher(idTexto).matches()) {
            enviar(ex, 400, obj("error", "El id de la cuenta debe ser numérico."));
            return;
        }
        Map<String, Object> fila = filtrar(leerJson(ex), CAMPOS_CUENTA);
        long id = Long.parseLong(idTexto);
        fila.put("id", id);
        if (!nombreValido(fila) || !saldoValido(fila)) {
            enviar(ex, 400, obj("error", "Se requiere nombre y saldo."));
            return;
        }
        escribir(ex, "POST", "cuentas", "on_conflict=id", fila, UPSERT, 200,
                obj("ok", true, "operacion", "actualizar", "id", id));
    }

    static void eliminarCuenta(HttpExchange ex, String idTexto) throws IOException {
        if (!SOLO_DIGITOS.matcher(idTexto).matches()) {
            enviar(ex, 400, obj("error", "El id de la cuenta debe ser numérico."));
            return;
        }
        long id = Long.parseLong(idTexto);
        escribir(ex, "DELETE", "cuentas", "id=eq." + id, null, null, 200,
                obj("ok", true, "operacion", "eliminar", "id", id));
    }

    static void insertarTransaccion(HttpExchange ex) throws IOException {
        Map<String, Object> fila = filtrar(leerJson(ex), CAMPOS_TRANSACCION);
        String tipo = texto(fila.get("tipo"));
        boolean tipoOk = tipo.equals("deposito") || tipo.equals("retiro") || tipo.equals("transferencia");
        if (!esId(fila.get("id")) || !tipoOk || numero(fila.get("monto")) == null) {
            enviar(ex, 400, obj("error", "Se requiere id, tipo (deposito/retiro/transferencia) y monto."));
            return;
        }
        escribir(ex, "POST", "transacciones", "on_conflict=id", fila, UPSERT, 201,
                obj("ok", true, "operacion", "insertar", "id", fila.get("id")));
    }

    // -----------------------------------------------------------------
    // Servidor y rutas
    // -----------------------------------------------------------------
    static void manejar(HttpExchange ex) throws IOException {
        String ruta = ex.getRequestURI().getPath();
        String metodo = ex.getRequestMethod();
        try {
            if (metodo.equals("GET") && ruta.equals("/")) {
                enviar(ex, 200, obj("status", "ok", "servicio", "microservicio-java",
                        "uso", "GET /api/tasa, POST /api/preguntar, POST /api/cuentas, PUT|DELETE /api/cuentas/{id}, POST /api/transacciones"));
            } else if (metodo.equals("GET") && ruta.equals("/api/tasa")) {
                obtenerTasa(ex);
            } else if (metodo.equals("POST") && ruta.equals("/api/preguntar")) {
                preguntarIA(ex);
            } else if (metodo.equals("POST") && ruta.equals("/api/cuentas")) {
                insertarCuenta(ex);
            } else if (metodo.equals("POST") && ruta.equals("/api/transacciones")) {
                insertarTransaccion(ex);
            } else if (ruta.startsWith("/api/cuentas/") && metodo.equals("PUT")) {
                actualizarCuenta(ex, ruta.substring("/api/cuentas/".length()));
            } else if (ruta.startsWith("/api/cuentas/") && metodo.equals("DELETE")) {
                eliminarCuenta(ex, ruta.substring("/api/cuentas/".length()));
            } else {
                enviar(ex, 404, obj("error", "Ruta no encontrada."));
            }
        } catch (IllegalArgumentException e) {
            enviar(ex, 400, obj("error", "El cuerpo no es un JSON válido."));
        } catch (Exception e) {
            e.printStackTrace();
            enviar(ex, 500, obj("error", "Error interno del servidor."));
        } finally {
            ex.close();
        }
    }

    public static void main(String[] args) throws Exception {
        int puerto = Integer.parseInt(env("PORT", "8080"));
        HttpServer servidor = HttpServer.create(new InetSocketAddress(puerto), 0);
        servidor.createContext("/", Main::manejar);
        servidor.setExecutor(Executors.newFixedThreadPool(16));
        servidor.start();
        System.out.println("microservicio-java escuchando en el puerto " + puerto);
    }
}
