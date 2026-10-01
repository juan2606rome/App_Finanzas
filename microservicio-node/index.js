// ruta: microservicio-node/index.js
// Microservicio en Node.js (sin librerías externas, solo Node 18+).
// Hace lo mismo que el microservicio de Python:
//   GET    /api/tasa              -> tasas de cambio (todas las monedas)
//   POST   /api/preguntar         -> IA (Mistral) con contexto real de Supabase
//   POST   /api/cuentas           -> INSERTAR una cuenta
//   PUT    /api/cuentas/:id       -> ACTUALIZAR una cuenta
//   DELETE /api/cuentas/:id       -> ELIMINAR una cuenta
//   POST   /api/transacciones     -> INSERTAR una transacción (historial)
"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");

// Documentación Swagger: /docs (interfaz) y /swagger.json (especificación OpenAPI).
const SWAGGER_JSON = fs.readFileSync(path.join(__dirname, "swagger.json"), "utf8");
const DOCS_HTML = `<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Swagger - Microservicio Finanzas</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>window.ui = SwaggerUIBundle({ url: "/swagger.json", dom_id: "#swagger-ui" });</script>
</body></html>`;

// ---------------------------------------------------------------------
// Configuración (variables de entorno, igual que en Render)
// ---------------------------------------------------------------------
// Aceptamos la URL con o sin "/rest/v1" al final, para evitar el error
// de la URL duplicada que ya nos pasó una vez.
function normalizarUrl(url) {
  let u = (url || "").trim().replace(/\/+$/, "");
  u = u.replace(/\/rest\/v1$/i, "");
  return u.replace(/\/+$/, "");
}

const SUPABASE_URL = normalizarUrl(process.env.SUPABASE_URL);
const SUPABASE_KEY = (process.env.SUPABASE_KEY || "").trim();
const MISTRAL_API_KEY = (process.env.MISTRAL_API_KEY || "").trim();
const MISTRAL_URL = process.env.MISTRAL_URL || "https://api.mistral.ai/v1/chat/completions";
const PORT = process.env.PORT || 3000;
const MAX_MOVIMIENTOS_CONTEXTO = 15;

const CAMPOS_CUENTA = ["id", "nombre", "saldo", "fecha_creacion", "actualizado_en"];
const CAMPOS_TRANSACCION = [
  "id", "tipo", "cuenta_origen_id", "cuenta_destino_id",
  "cuenta_origen_nombre", "cuenta_destino_nombre", "monto", "descripcion", "fecha",
];
// merge-duplicates: si el id ya existe, la fila se actualiza en vez de fallar.
const UPSERT = "resolution=merge-duplicates,return=minimal";

// ---------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------
function enviar(res, codigo, cuerpo) {
  const texto = JSON.stringify(cuerpo);
  res.writeHead(codigo, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(texto),
  });
  res.end(texto);
}

function leerJson(req) {
  return new Promise((resolve, reject) => {
    let datos = "";
    req.on("data", (trozo) => {
      datos += trozo;
      if (datos.length > 1e6) {
        req.destroy();
        reject(Object.assign(new Error("Cuerpo demasiado grande."), { esCliente: true }));
      }
    });
    req.on("end", () => {
      if (!datos) return resolve({});
      try {
        resolve(JSON.parse(datos));
      } catch {
        reject(Object.assign(new Error("El cuerpo no es un JSON válido."), { esCliente: true }));
      }
    });
    req.on("error", reject);
  });
}

function filtrar(obj, campos) {
  const salida = {};
  for (const c of campos) if (obj[c] !== undefined) salida[c] = obj[c];
  return salida;
}

const esId = (v) => /^\d+$/.test(String(v));

async function supabase(metodo, tabla, query = "", cuerpo = null, prefer = null) {
  const headers = { apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}` };
  if (cuerpo !== null) headers["Content-Type"] = "application/json";
  if (prefer) headers["Prefer"] = prefer;
  const url = `${SUPABASE_URL}/rest/v1/${tabla}${query ? "?" + query : ""}`;
  const r = await fetch(url, {
    method: metodo,
    headers,
    body: cuerpo !== null ? JSON.stringify(cuerpo) : undefined,
    signal: AbortSignal.timeout(6000),
  });
  const texto = await r.text();
  return { ok: r.ok, status: r.status, texto };
}

async function consultar(tabla, query) {
  const r = await supabase("GET", tabla, query);
  if (!r.ok) throw new Error(`Supabase respondió ${r.status} al consultar ${tabla}`);
  return JSON.parse(r.texto);
}

// Ejecuta una escritura en Supabase y responde con el resultado.
async function escribir(res, { metodo, tabla, query, cuerpo, prefer, exito }) {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    return enviar(res, 500, { error: "falta configurar SUPABASE_URL y SUPABASE_KEY." });
  }
  let r;
  try {
    r = await supabase(metodo, tabla, query, cuerpo, prefer);
  } catch (e) {
    return enviar(res, 502, { error: `No se pudo contactar a Supabase: ${e.message}` });
  }
  if (!r.ok) {
    return enviar(res, 502, { error: `Supabase respondió ${r.status}: ${r.texto.slice(0, 300)}` });
  }
  return enviar(res, exito.status, exito.cuerpo);
}

function formatearPesos(valor) {
  const n = Number(valor);
  if (!Number.isFinite(n)) return String(valor);
  return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

function formatearFecha(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return iso || "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)}/${d.getUTCFullYear()} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

// ---------------------------------------------------------------------
// LECTURA: tasas
// ---------------------------------------------------------------------
async function obtenerTasa(res) {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    return enviar(res, 500, { error: "falta configurar SUPABASE_URL y SUPABASE_KEY." });
  }
  let filas;
  try {
    filas = await consultar("tasas", "select=*&order=moneda.asc");
  } catch (e) {
    return enviar(res, 502, { error: `No se pudo consultar Supabase: ${e.message}` });
  }
  if (!filas.length) return enviar(res, 404, { error: "La tabla 'tasas' está vacía en Supabase." });

  const tasas = filas
    .filter((f) => f.moneda && f.tasa_cop !== null && f.tasa_cop !== undefined)
    .map((f) => ({
      moneda: f.moneda,
      tasa_cop: f.tasa_cop,
      fecha_actualizacion: f.fecha_actualizacion,
    }));
  return enviar(res, 200, { tasas });
}

// ---------------------------------------------------------------------
// LECTURA: IA con contexto financiero
// ---------------------------------------------------------------------
async function construirContexto() {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    return "(No hay conexión configurada a la base de datos del usuario.)";
  }
  const [cuentas, tasas, movimientos] = await Promise.all([
    consultar("cuentas", "select=nombre,saldo,actualizado_en&order=nombre.asc").catch(() => null),
    consultar("tasas", "select=*&order=moneda.asc").catch(() => null),
    consultar("transacciones", `select=*&order=fecha.desc&limit=${MAX_MOVIMIENTOS_CONTEXTO}`).catch(() => null),
  ]);

  const partes = [];

  if (cuentas === null) {
    partes.push("No se pudieron leer las cuentas del usuario en este momento.");
  } else if (cuentas.length === 0) {
    partes.push("El usuario todavía no tiene ninguna cuenta creada.");
  } else {
    const total = cuentas.reduce((s, c) => s + Number(c.saldo || 0), 0);
    const lineas = cuentas.map((c) => `- ${c.nombre}: $${formatearPesos(c.saldo)} COP`);
    partes.push(
      "Cuentas actuales del usuario (en pesos colombianos, COP):\n" +
        lineas.join("\n") +
        `\nTotal en todas las cuentas: $${formatearPesos(total)} COP`
    );
  }

  if (tasas && tasas.length) {
    const total = (cuentas || []).reduce((s, c) => s + Number(c.saldo || 0), 0);
    const lineas = [];
    for (const t of tasas) {
      if (!t.moneda || !t.tasa_cop) continue;
      const equivalente = Math.round((total / Number(t.tasa_cop)) * 100) / 100;
      lineas.push(
        `- 1 ${t.moneda} = $${formatearPesos(t.tasa_cop)} COP (el total del usuario equivale a ~${equivalente} ${t.moneda})`
      );
    }
    if (lineas.length) partes.push("Tasas de cambio actuales:\n" + lineas.join("\n"));
  }

  if (movimientos === null) {
    partes.push("No se pudo leer el historial de movimientos en este momento.");
  } else if (movimientos.length === 0) {
    partes.push("El usuario todavía no tiene movimientos registrados.");
  } else {
    const lineas = movimientos.map((m) => {
      const monto = formatearPesos(m.monto);
      const origen = m.cuenta_origen_nombre || "(cuenta eliminada)";
      const destino = m.cuenta_destino_nombre || "(cuenta eliminada)";
      const descripcion = (m.descripcion || "").trim();
      let detalle;
      if (m.tipo === "transferencia") detalle = `transferencia de $${monto} de ${origen} a ${destino}`;
      else if (m.tipo === "deposito") detalle = `depósito de $${monto} en ${destino}`;
      else if (m.tipo === "retiro") detalle = `retiro de $${monto} de ${origen}`;
      else detalle = `${m.tipo} de $${monto}`;
      if (descripcion) detalle += ` (descripción: ${descripcion})`;
      return `- ${formatearFecha(m.fecha)}: ${detalle}`;
    });
    partes.push(
      `Últimos ${lineas.length} movimientos del usuario (el primero es el más reciente):\n` + lineas.join("\n")
    );
  }

  return partes.join("\n\n");
}

const PROMPT_SISTEMA =
  "Eres el asistente de una app de finanzas personales que " +
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
  "=== INFORMACIÓN ACTUAL DEL USUARIO ===\n";

async function preguntarIA(req, res) {
  if (!MISTRAL_API_KEY) return enviar(res, 500, { error: "Falta configurar MISTRAL_API_KEY." });

  const datos = await leerJson(req);
  const pregunta = String(datos.pregunta || "").trim();
  if (!pregunta) return enviar(res, 400, { error: "No se recibió ninguna pregunta." });

  const contexto = await construirContexto();

  try {
    const r = await fetch(MISTRAL_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${MISTRAL_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "ministral-8b-2512",
        messages: [
          { role: "system", content: PROMPT_SISTEMA + contexto },
          { role: "user", content: pregunta },
        ],
        temperature: 0.3,
      }),
      signal: AbortSignal.timeout(15000),
    });
    if (!r.ok) return enviar(res, 502, { error: `No se pudo contactar a Mistral: HTTP ${r.status}` });
    const json = await r.json();
    const texto = json.choices[0].message.content;
    return enviar(res, 200, { respuesta: texto });
  } catch (e) {
    return enviar(res, 502, { error: `No se pudo contactar a Mistral: ${e.message}` });
  }
}

// ---------------------------------------------------------------------
// ESCRITURA: insertar / actualizar / eliminar
// ---------------------------------------------------------------------
async function insertarCuenta(req, res) {
  const fila = filtrar(await leerJson(req), CAMPOS_CUENTA);
  if (!esId(fila.id) || typeof fila.nombre !== "string" || !fila.nombre.trim() ||
      fila.saldo === undefined || isNaN(Number(fila.saldo))) {
    return enviar(res, 400, { error: "Se requiere id, nombre y saldo." });
  }
  return escribir(res, {
    metodo: "POST", tabla: "cuentas", query: "on_conflict=id", cuerpo: fila, prefer: UPSERT,
    exito: { status: 201, cuerpo: { ok: true, operacion: "insertar", id: fila.id } },
  });
}

async function actualizarCuenta(req, res, idTexto) {
  if (!esId(idTexto)) return enviar(res, 400, { error: "El id de la cuenta debe ser numérico." });
  const fila = filtrar(await leerJson(req), CAMPOS_CUENTA);
  fila.id = Number(idTexto);
  if (typeof fila.nombre !== "string" || !fila.nombre.trim() ||
      fila.saldo === undefined || isNaN(Number(fila.saldo))) {
    return enviar(res, 400, { error: "Se requiere nombre y saldo." });
  }
  return escribir(res, {
    metodo: "POST", tabla: "cuentas", query: "on_conflict=id", cuerpo: fila, prefer: UPSERT,
    exito: { status: 200, cuerpo: { ok: true, operacion: "actualizar", id: fila.id } },
  });
}

async function eliminarCuenta(res, idTexto) {
  if (!esId(idTexto)) return enviar(res, 400, { error: "El id de la cuenta debe ser numérico." });
  return escribir(res, {
    metodo: "DELETE", tabla: "cuentas", query: `id=eq.${Number(idTexto)}`, cuerpo: null, prefer: null,
    exito: { status: 200, cuerpo: { ok: true, operacion: "eliminar", id: Number(idTexto) } },
  });
}

async function insertarTransaccion(req, res) {
  const fila = filtrar(await leerJson(req), CAMPOS_TRANSACCION);
  if (!esId(fila.id) || !["deposito", "retiro", "transferencia"].includes(fila.tipo) ||
      fila.monto === undefined || isNaN(Number(fila.monto))) {
    return enviar(res, 400, { error: "Se requiere id, tipo (deposito/retiro/transferencia) y monto." });
  }
  return escribir(res, {
    metodo: "POST", tabla: "transacciones", query: "on_conflict=id", cuerpo: fila, prefer: UPSERT,
    exito: { status: 201, cuerpo: { ok: true, operacion: "insertar", id: fila.id } },
  });
}

// ---------------------------------------------------------------------
// Servidor y rutas
// ---------------------------------------------------------------------
const servidor = http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, "http://localhost");
  const metodo = req.method;
  try {
    if (metodo === "GET" && pathname === "/") {
      return enviar(res, 200, {
        status: "ok",
        servicio: "microservicio-nodejs",
        uso: "GET /api/tasa, POST /api/preguntar, POST /api/cuentas, PUT|DELETE /api/cuentas/:id, POST /api/transacciones",
        documentacion: "/docs",
      });
    }
    if (metodo === "GET" && pathname === "/swagger.json") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      return res.end(SWAGGER_JSON);
    }
    if (metodo === "GET" && (pathname === "/docs" || pathname === "/docs/")) {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return res.end(DOCS_HTML);
    }
    if (metodo === "GET" && pathname === "/api/tasa") return await obtenerTasa(res);
    if (metodo === "POST" && pathname === "/api/preguntar") return await preguntarIA(req, res);
    if (metodo === "POST" && pathname === "/api/cuentas") return await insertarCuenta(req, res);
    if (metodo === "POST" && pathname === "/api/transacciones") return await insertarTransaccion(req, res);

    const m = pathname.match(/^\/api\/cuentas\/([^/]+)$/);
    if (m && metodo === "PUT") return await actualizarCuenta(req, res, m[1]);
    if (m && metodo === "DELETE") return await eliminarCuenta(res, m[1]);

    return enviar(res, 404, { error: "Ruta no encontrada." });
  } catch (e) {
    if (e.esCliente) return enviar(res, 400, { error: e.message });
    console.error(e);
    return enviar(res, 500, { error: "Error interno del servidor." });
  }
});

servidor.listen(PORT, "0.0.0.0", () => console.log(`microservicio-nodejs escuchando en el puerto ${PORT}`));