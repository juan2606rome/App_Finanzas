// ruta: microservicio-go/main.go
// Microservicio en Go (solo librería estándar). Hace lo mismo que los de
// Python, Node.js y Java:
//
//	GET    /api/tasa              -> tasas de cambio (todas las monedas)
//	POST   /api/preguntar         -> IA (Mistral) con contexto real de Supabase
//	POST   /api/cuentas           -> INSERTAR una cuenta
//	PUT    /api/cuentas/{id}      -> ACTUALIZAR una cuenta
//	DELETE /api/cuentas/{id}      -> ELIMINAR una cuenta
//	POST   /api/transacciones     -> INSERTAR una transacción (historial)
package main

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// ---------------------------------------------------------------------
// Configuración (variables de entorno, igual que en Render)
// ---------------------------------------------------------------------

var reRestV1 = regexp.MustCompile(`(?i)/rest/v1$`)

// Acepta la URL con o sin "/rest/v1" al final (para evitar la URL duplicada).
func normalizarURL(u string) string {
	u = strings.TrimRight(strings.TrimSpace(u), "/")
	u = reRestV1.ReplaceAllString(u, "")
	return strings.TrimRight(u, "/")
}

func envOr(clave, defecto string) string {
	if v := strings.TrimSpace(os.Getenv(clave)); v != "" {
		return v
	}
	return defecto
}

var (
	supabaseURL = normalizarURL(os.Getenv("SUPABASE_URL"))
	supabaseKey = strings.TrimSpace(os.Getenv("SUPABASE_KEY"))
	mistralKey  = strings.TrimSpace(os.Getenv("MISTRAL_API_KEY"))
	mistralURL  = envOr("MISTRAL_URL", "https://api.mistral.ai/v1/chat/completions")

	clienteSupabase = &http.Client{Timeout: 6 * time.Second}
	clienteMistral  = &http.Client{Timeout: 15 * time.Second}

	camposCuenta      = []string{"id", "nombre", "saldo", "fecha_creacion", "actualizado_en"}
	camposTransaccion = []string{"id", "tipo", "cuenta_origen_id", "cuenta_destino_id", "cuenta_origen_nombre", "cuenta_destino_nombre", "monto", "descripcion", "fecha"}
	reID              = regexp.MustCompile(`^\d+$`)
)

const (
	maxMovimientosContexto = 15
	// merge-duplicates: si el id ya existe, la fila se actualiza en vez de fallar.
	upsert = "resolution=merge-duplicates,return=minimal"
)

type obj = map[string]any

// Documentación Swagger: /docs (interfaz) y /swagger.json (especificación OpenAPI).
// El archivo swagger.json (misma carpeta que main.go) se incrusta en el binario.
//
//go:embed swagger.json
var swaggerJSON []byte

const docsHTML = `<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Swagger - Microservicio Finanzas</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>window.ui = SwaggerUIBundle({ url: "/swagger.json", dom_id: "#swagger-ui" });</script>
</body></html>`

// ---------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------

func enviar(w http.ResponseWriter, codigo int, cuerpo any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(codigo)
	_ = json.NewEncoder(w).Encode(cuerpo)
}

// Lee el cuerpo JSON conservando los números tal cual (json.Number).
func leerJSON(r *http.Request) (obj, error) {
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.UseNumber()
	datos := obj{}
	if err := dec.Decode(&datos); err != nil {
		if err == io.EOF {
			return datos, nil
		}
		return nil, err
	}
	return datos, nil
}

func filtrar(origen obj, campos []string) obj {
	salida := obj{}
	for _, c := range campos {
		if v, ok := origen[c]; ok {
			salida[c] = v
		}
	}
	return salida
}

func texto(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case json.Number:
		return t.String()
	default:
		return fmt.Sprint(t)
	}
}

func numero(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case json.Number:
		f, err := t.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(t, 64)
		return f, err == nil
	}
	return 0, false
}

func esID(v any) bool { return reID.MatchString(texto(v)) }

func supabase(metodo, tabla, query string, cuerpo any, prefer string) (int, []byte, error) {
	url := supabaseURL + "/rest/v1/" + tabla
	if query != "" {
		url += "?" + query
	}
	var lector io.Reader
	if cuerpo != nil {
		b, err := json.Marshal(cuerpo)
		if err != nil {
			return 0, nil, err
		}
		lector = bytes.NewReader(b)
	}
	req, err := http.NewRequest(metodo, url, lector)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("apikey", supabaseKey)
	req.Header.Set("Authorization", "Bearer "+supabaseKey)
	if cuerpo != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if prefer != "" {
		req.Header.Set("Prefer", prefer)
	}
	resp, err := clienteSupabase.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data, nil
}

func consultar(tabla, query string) ([]obj, error) {
	status, data, err := supabase("GET", tabla, query, nil, "")
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("Supabase respondió %d al consultar %s", status, tabla)
	}
	var filas []obj
	if err := json.Unmarshal(data, &filas); err != nil {
		return nil, err
	}
	return filas, nil
}

// Ejecuta una escritura en Supabase y responde con el resultado.
func escribir(w http.ResponseWriter, metodo, tabla, query string, cuerpo any, prefer string, codigoOK int, respuestaOK obj) {
	if supabaseURL == "" || supabaseKey == "" {
		enviar(w, 500, obj{"error": "falta configurar SUPABASE_URL y SUPABASE_KEY."})
		return
	}
	status, data, err := supabase(metodo, tabla, query, cuerpo, prefer)
	if err != nil {
		enviar(w, 502, obj{"error": "No se pudo contactar a Supabase: " + err.Error()})
		return
	}
	if status < 200 || status >= 300 {
		detalle := string(data)
		if len(detalle) > 300 {
			detalle = detalle[:300]
		}
		enviar(w, 502, obj{"error": fmt.Sprintf("Supabase respondió %d: %s", status, detalle)})
		return
	}
	enviar(w, codigoOK, respuestaOK)
}

func formatearPesos(v any) string {
	n, ok := numero(v)
	if !ok {
		return texto(v)
	}
	entero := int64(math.Round(math.Abs(n)))
	s := strconv.FormatInt(entero, 10)
	var sb strings.Builder
	for i, c := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			sb.WriteByte('.')
		}
		sb.WriteRune(c)
	}
	if n < 0 {
		return "-" + sb.String()
	}
	return sb.String()
}

func formatearFecha(v any) string {
	s := texto(v)
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return s
	}
	return t.Format("02/01/2006 15:04")
}

func sumaSaldos(cuentas []obj) float64 {
	total := 0.0
	for _, c := range cuentas {
		if n, ok := numero(c["saldo"]); ok {
			total += n
		}
	}
	return total
}

// ---------------------------------------------------------------------
// LECTURA: tasas
// ---------------------------------------------------------------------

func obtenerTasa(w http.ResponseWriter) {
	if supabaseURL == "" || supabaseKey == "" {
		enviar(w, 500, obj{"error": "falta configurar SUPABASE_URL y SUPABASE_KEY."})
		return
	}
	filas, err := consultar("tasas", "select=*&order=moneda.asc")
	if err != nil {
		enviar(w, 502, obj{"error": "No se pudo consultar Supabase: " + err.Error()})
		return
	}
	if len(filas) == 0 {
		enviar(w, 404, obj{"error": "La tabla 'tasas' está vacía en Supabase."})
		return
	}
	tasas := []obj{}
	for _, f := range filas {
		if texto(f["moneda"]) == "" || f["tasa_cop"] == nil {
			continue
		}
		tasas = append(tasas, obj{
			"moneda":              f["moneda"],
			"tasa_cop":            f["tasa_cop"],
			"fecha_actualizacion": f["fecha_actualizacion"],
		})
	}
	enviar(w, 200, obj{"tasas": tasas})
}

// ---------------------------------------------------------------------
// LECTURA: IA con contexto financiero
// ---------------------------------------------------------------------

func construirContexto() string {
	if supabaseURL == "" || supabaseKey == "" {
		return "(No hay conexión configurada a la base de datos del usuario.)"
	}
	cuentas, errC := consultar("cuentas", "select=nombre,saldo,actualizado_en&order=nombre.asc")
	tasas, errT := consultar("tasas", "select=*&order=moneda.asc")
	movs, errM := consultar("transacciones", fmt.Sprintf("select=*&order=fecha.desc&limit=%d", maxMovimientosContexto))

	partes := []string{}

	if errC != nil {
		partes = append(partes, "No se pudieron leer las cuentas del usuario en este momento.")
	} else if len(cuentas) == 0 {
		partes = append(partes, "El usuario todavía no tiene ninguna cuenta creada.")
	} else {
		lineas := []string{}
		for _, c := range cuentas {
			lineas = append(lineas, fmt.Sprintf("- %s: $%s COP", texto(c["nombre"]), formatearPesos(c["saldo"])))
		}
		partes = append(partes, "Cuentas actuales del usuario (en pesos colombianos, COP):\n"+
			strings.Join(lineas, "\n")+
			fmt.Sprintf("\nTotal en todas las cuentas: $%s COP", formatearPesos(sumaSaldos(cuentas))))
	}

	if errT == nil && len(tasas) > 0 {
		total := 0.0
		if errC == nil {
			total = sumaSaldos(cuentas)
		}
		lineas := []string{}
		for _, t := range tasas {
			tasa, ok := numero(t["tasa_cop"])
			moneda := texto(t["moneda"])
			if !ok || tasa == 0 || moneda == "" {
				continue
			}
			equivalente := math.Round(total/tasa*100) / 100
			lineas = append(lineas, fmt.Sprintf("- 1 %s = $%s COP (el total del usuario equivale a ~%s %s)",
				moneda, formatearPesos(tasa), strconv.FormatFloat(equivalente, 'f', -1, 64), moneda))
		}
		if len(lineas) > 0 {
			partes = append(partes, "Tasas de cambio actuales:\n"+strings.Join(lineas, "\n"))
		}
	}

	if errM != nil {
		partes = append(partes, "No se pudo leer el historial de movimientos en este momento.")
	} else if len(movs) == 0 {
		partes = append(partes, "El usuario todavía no tiene movimientos registrados.")
	} else {
		lineas := []string{}
		for _, m := range movs {
			monto := formatearPesos(m["monto"])
			origen := texto(m["cuenta_origen_nombre"])
			if origen == "" {
				origen = "(cuenta eliminada)"
			}
			destino := texto(m["cuenta_destino_nombre"])
			if destino == "" {
				destino = "(cuenta eliminada)"
			}
			var detalle string
			switch texto(m["tipo"]) {
			case "transferencia":
				detalle = fmt.Sprintf("transferencia de $%s de %s a %s", monto, origen, destino)
			case "deposito":
				detalle = fmt.Sprintf("depósito de $%s en %s", monto, destino)
			case "retiro":
				detalle = fmt.Sprintf("retiro de $%s de %s", monto, origen)
			default:
				detalle = fmt.Sprintf("%s de $%s", texto(m["tipo"]), monto)
			}
			if desc := strings.TrimSpace(texto(m["descripcion"])); desc != "" {
				detalle += fmt.Sprintf(" (descripción: %s)", desc)
			}
			lineas = append(lineas, fmt.Sprintf("- %s: %s", formatearFecha(m["fecha"]), detalle))
		}
		partes = append(partes, fmt.Sprintf("Últimos %d movimientos del usuario (el primero es el más reciente):\n", len(lineas))+
			strings.Join(lineas, "\n"))
	}

	return strings.Join(partes, "\n\n")
}

const promptSistema = "Eres el asistente de una app de finanzas personales que " +
	"usa pesos colombianos (COP). A continuación tienes la " +
	"información real y actualizada de las cuentas, tasas de " +
	"cambio y movimientos del usuario. Úsala para responder " +
	"sus preguntas con datos exactos (por ejemplo, cuánto " +
	"dinero tiene en total, a cuántos dólares equivale, o " +
	"cuáles fueron sus últimos movimientos). Si la pregunta " +
	"no tiene que ver con esta información, respóndela igual " +
	"con tus conocimientos generales. Si no encuentras el " +
	"dato exacto que te piden en la información de abajo, " +
	"dilo claramente en vez de inventarlo. Responde en " +
	"español, corto y claro.\n\n" +
	"=== INFORMACIÓN ACTUAL DEL USUARIO ===\n"

func preguntarIA(w http.ResponseWriter, r *http.Request) {
	if mistralKey == "" {
		enviar(w, 500, obj{"error": "Falta configurar MISTRAL_API_KEY."})
		return
	}
	datos, err := leerJSON(r)
	if err != nil {
		enviar(w, 400, obj{"error": "El cuerpo no es un JSON válido."})
		return
	}
	pregunta := strings.TrimSpace(texto(datos["pregunta"]))
	if pregunta == "" {
		enviar(w, 400, obj{"error": "No se recibió ninguna pregunta."})
		return
	}

	contexto := construirContexto()

	cuerpo, _ := json.Marshal(obj{
		"model": "ministral-8b-2512",
		"messages": []obj{
			{"role": "system", "content": promptSistema + contexto},
			{"role": "user", "content": pregunta},
		},
		"temperature": 0.3,
	})
	req, _ := http.NewRequest("POST", mistralURL, bytes.NewReader(cuerpo))
	req.Header.Set("Authorization", "Bearer "+mistralKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := clienteMistral.Do(req)
	if err != nil {
		enviar(w, 502, obj{"error": "No se pudo contactar a Mistral: " + err.Error()})
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		enviar(w, 502, obj{"error": fmt.Sprintf("No se pudo contactar a Mistral: HTTP %d", resp.StatusCode)})
		return
	}
	var salida struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&salida); err != nil || len(salida.Choices) == 0 {
		enviar(w, 502, obj{"error": "Respuesta inesperada de Mistral."})
		return
	}
	enviar(w, 200, obj{"respuesta": salida.Choices[0].Message.Content})
}

// ---------------------------------------------------------------------
// ESCRITURA: insertar / actualizar / eliminar
// ---------------------------------------------------------------------

func nombreValido(fila obj) bool {
	return strings.TrimSpace(texto(fila["nombre"])) != ""
}

func saldoValido(fila obj) bool {
	_, ok := numero(fila["saldo"])
	return ok
}

func insertarCuenta(w http.ResponseWriter, r *http.Request) {
	datos, err := leerJSON(r)
	if err != nil {
		enviar(w, 400, obj{"error": "El cuerpo no es un JSON válido."})
		return
	}
	fila := filtrar(datos, camposCuenta)
	if !esID(fila["id"]) || !nombreValido(fila) || !saldoValido(fila) {
		enviar(w, 400, obj{"error": "Se requiere id, nombre y saldo."})
		return
	}
	escribir(w, "POST", "cuentas", "on_conflict=id", fila, upsert, 201,
		obj{"ok": true, "operacion": "insertar", "id": fila["id"]})
}

func actualizarCuenta(w http.ResponseWriter, r *http.Request, idTexto string) {
	if !reID.MatchString(idTexto) {
		enviar(w, 400, obj{"error": "El id de la cuenta debe ser numérico."})
		return
	}
	datos, err := leerJSON(r)
	if err != nil {
		enviar(w, 400, obj{"error": "El cuerpo no es un JSON válido."})
		return
	}
	fila := filtrar(datos, camposCuenta)
	id, _ := strconv.ParseInt(idTexto, 10, 64)
	fila["id"] = id
	if !nombreValido(fila) || !saldoValido(fila) {
		enviar(w, 400, obj{"error": "Se requiere nombre y saldo."})
		return
	}
	escribir(w, "POST", "cuentas", "on_conflict=id", fila, upsert, 200,
		obj{"ok": true, "operacion": "actualizar", "id": id})
}

func eliminarCuenta(w http.ResponseWriter, idTexto string) {
	if !reID.MatchString(idTexto) {
		enviar(w, 400, obj{"error": "El id de la cuenta debe ser numérico."})
		return
	}
	id, _ := strconv.ParseInt(idTexto, 10, 64)
	escribir(w, "DELETE", "cuentas", fmt.Sprintf("id=eq.%d", id), nil, "", 200,
		obj{"ok": true, "operacion": "eliminar", "id": id})
}

func insertarTransaccion(w http.ResponseWriter, r *http.Request) {
	datos, err := leerJSON(r)
	if err != nil {
		enviar(w, 400, obj{"error": "El cuerpo no es un JSON válido."})
		return
	}
	fila := filtrar(datos, camposTransaccion)
	tipo := texto(fila["tipo"])
	_, montoOK := numero(fila["monto"])
	if !esID(fila["id"]) || !(tipo == "deposito" || tipo == "retiro" || tipo == "transferencia") || !montoOK {
		enviar(w, 400, obj{"error": "Se requiere id, tipo (deposito/retiro/transferencia) y monto."})
		return
	}
	escribir(w, "POST", "transacciones", "on_conflict=id", fila, upsert, 201,
		obj{"ok": true, "operacion": "insertar", "id": fila["id"]})
}

// ---------------------------------------------------------------------
// Servidor y rutas
// ---------------------------------------------------------------------

func rutas(w http.ResponseWriter, r *http.Request) {
	ruta := r.URL.Path
	metodo := r.Method

	switch {
	case metodo == "GET" && ruta == "/":
		enviar(w, 200, obj{
			"status":   "ok",
			"servicio": "microservicio-go",
			"uso":      "GET /api/tasa, POST /api/preguntar, POST /api/cuentas, PUT|DELETE /api/cuentas/{id}, POST /api/transacciones",
			"documentacion": "/docs",
		})
	case metodo == "GET" && ruta == "/swagger.json":
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write(swaggerJSON)
	case metodo == "GET" && (ruta == "/docs" || ruta == "/docs/"):
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(docsHTML))
	case metodo == "GET" && ruta == "/api/tasa":
		obtenerTasa(w)
	case metodo == "POST" && ruta == "/api/preguntar":
		preguntarIA(w, r)
	case metodo == "POST" && ruta == "/api/cuentas":
		insertarCuenta(w, r)
	case metodo == "POST" && ruta == "/api/transacciones":
		insertarTransaccion(w, r)
	case strings.HasPrefix(ruta, "/api/cuentas/") && metodo == "PUT":
		actualizarCuenta(w, r, strings.TrimPrefix(ruta, "/api/cuentas/"))
	case strings.HasPrefix(ruta, "/api/cuentas/") && metodo == "DELETE":
		eliminarCuenta(w, strings.TrimPrefix(ruta, "/api/cuentas/"))
	default:
		enviar(w, 404, obj{"error": "Ruta no encontrada."})
	}
}

func main() {
	puerto := envOr("PORT", "8080")
	http.HandleFunc("/", rutas)
	log.Printf("microservicio-go escuchando en el puerto %s", puerto)
	log.Fatal(http.ListenAndServe(":"+puerto, nil))
}