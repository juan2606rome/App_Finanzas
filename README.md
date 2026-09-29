# 💰 Mis Finanzas — App de finanzas personales con microservicios

Aplicación web para llevar el control de tu plata: crea tus cuentas (Nequi, Nu, Bancolombia, efectivo…), deposita, retira y transfiere entre ellas, mira tu historial, consulta cuánto equivale tu dinero en otras monedas y **pregúntale a una IA** por tus finanzas.

**Cualquier persona puede usarla a su gusto**: puedes abrir la página publicada, o clonar el repositorio, correrla en tu computador y desplegar tu propia copia con tus cuentas y tus claves.

🌐 **Página publicada:** https://app-finanzas-pagina.onrender.com
> Está en el plan gratis de Render: si nadie la usó en un rato, la primera carga puede tardar hasta ~1 minuto.

---

## ✨ ¿Qué puedes hacer?

| Pestaña | Qué hace |
|---|---|
| **Inicio** | Total de tu dinero y lista de cuentas. Crear, editar y eliminar cuentas. |
| **Depositar / Retirar** | Suma o resta dinero a una cuenta (no deja retirar más de lo que hay). |
| **Transferir** | Mueve dinero entre tus propias cuentas. |
| **Historial** | Todos los movimientos, del más reciente al más antiguo. |
| **Cotización USD** | Tasas de cambio (USD y las monedas que agregues) y a cuánto equivale tu total. |
| **Chat con IA** | Preguntas como *"¿cuántos dólares puedo tener?"* o *"¿cuáles fueron mis últimas transferencias?"*. La IA (Mistral) lee tus datos reales antes de responder. |
| **Microservicios** | Estado de cada microservicio, botón para encenderlos y para sincronizar con Supabase. |

## 🧱 Cómo está armado

```
Navegador ──► Django (front + back, base propia: SQLite / PostgreSQL)
                 │
                 │  llama, en este orden, al primero que responda:
                 ▼
        Python (Flask) ─► Java ─► Node.js ─► Go      (los 4 hacen lo mismo)
                 │
                 ▼
     Supabase (copia de cuentas y movimientos, tasas)  +  Mistral (IA)
```

- **Django** guarda la información "de verdad" (SQLite en tu PC, PostgreSQL en Render).
- **Cuatro microservicios equivalentes** (Python, Java, Node.js y Go) exponen las mismas rutas: tasas, IA, e insertar / actualizar / eliminar en Supabase.
- **Resiliencia (failover):** si el de Python falla, Django pasa al de Java, luego a Node.js y por último a Go.
- **Bandeja de sincronización:** cada cambio se guarda primero en una bandeja (`SyncPendiente`). Si los microservicios están apagados no se pierde nada: **apenas uno enciende, Django sube todo a Supabase automáticamente**. El botón *Sincronizar todo* sigue ahí por si quieres forzarlo.
- **Sin envíos dobles:** al enviar cualquier formulario (Chat con IA, crear cuenta, transferir…) el botón se bloquea y muestra un loader hasta que llega la respuesta.

## 📁 Estructura del repositorio

```
finanzas/               App Django (manage.py, apps: cuentas, tasas, ia, servicios)
microservicio/          Microservicio Python (Flask)   → /docs = Swagger
microservicio-java/     Microservicio Java (Dockerfile)
microservicio-node/     Microservicio Node.js
microservicio-go/       Microservicio Go
```

---

## 🚀 Usarla en tu computador

### Requisitos
- Python 3.11 o superior
- (Opcional, solo si quieres correr los microservicios tú mismo) Node.js 18+, Go 1.21+, Java 21 / Docker

### 1) Clonar e instalar

```bash
git clone https://github.com/juan2606rome/App_Finanzas.git
cd App_Finanzas

python -m venv venv
# Windows:
venv\Scripts\activate
# Mac / Linux:
source venv/bin/activate

pip install -r finanzas/requirements.txt
```

### 2) Configurar el archivo `.env`

Crea `finanzas/.env` (junto a `manage.py`). **Nunca lo subas a GitHub** (ya está en `.gitignore`).

```env
# URLs de los microservicios, sin "/" al final. Se prueban en este orden.
# Si dejas una vacía, se salta.
MS_PYTHON_URL=https://tu-servicio-python.onrender.com
MS_JAVA_URL=https://tu-servicio-java.onrender.com
MS_NODE_URL=https://tu-servicio-node.onrender.com
MS_GO_URL=https://tu-servicio-go.onrender.com
```

> Si no pones nada, se usan las URLs de la instancia del autor. Para tener **tus propios datos** despliega tus microservicios y tu Supabase (ver más abajo) y pon tus URLs aquí.

### 3) Crear la base de datos y arrancar

```bash
cd finanzas
python manage.py migrate
python manage.py runserver
```

Abre **http://127.0.0.1:8000** 🎉

Comandos útiles:

```bash
python manage.py createsuperuser   # crear usuario para /admin
python manage.py test              # correr pruebas
python manage.py makemigrations    # después de cambiar modelos
```

---

## 🔌 Correr los microservicios

Todos necesitan estas variables de entorno (ahí sí van las claves):

| Variable | Para qué |
|---|---|
| `SUPABASE_URL` | URL de tu proyecto Supabase |
| `SUPABASE_KEY` | Clave de Supabase |
| `MISTRAL_API_KEY` | Clave de la API de Mistral (para el chat con IA) |
| `MISTRAL_URL` | *(opcional)* por defecto `https://api.mistral.ai/v1/chat/completions` |
| `PORT` | *(opcional)* puerto; Render lo define solo |

**Python (Flask)**
```bash
cd microservicio
pip install -r requirements.txt
python app.py                # http://localhost:5000  (Swagger en /docs)
# producción: gunicorn app:app
```

**Node.js**
```bash
cd microservicio-node
npm start                    # puerto 3000
```

**Go**
```bash
cd microservicio-go
go run .                     # puerto 8080
```

**Java** (con Docker)
```bash
cd microservicio-java
docker build -t ms-finanzas-java .
docker run -p 8080:8080 -e SUPABASE_URL=... -e SUPABASE_KEY=... -e MISTRAL_API_KEY=... ms-finanzas-java
```

### Rutas que exponen los 4 microservicios

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Estado del servicio (así se sabe si está encendido) |
| GET | `/api/tasa` | Tasas de cambio desde Supabase |
| POST | `/api/preguntar` | Pregunta a la IA con el contexto de tus finanzas |
| POST | `/api/cuentas` | **Insertar** cuenta |
| PUT | `/api/cuentas/<id>` | **Actualizar** cuenta |
| DELETE | `/api/cuentas/<id>` | **Eliminar** cuenta |
| POST | `/api/transacciones` | **Insertar** movimiento |

### Tablas de Supabase

Crea estas tablas en tu proyecto: `cuentas`, `transacciones` y `tasas` (`moneda`, `tasa_cop`, `fecha_actualizacion`). Para agregar una moneda nueva (ej. EUR) basta con añadir una fila en `tasas`: aparece sola en la página.

---

## ⚙️ Variables opcionales de Django

| Variable | Por defecto | Qué hace |
|---|---|---|
| `SECRET_KEY` | *(clave de desarrollo)* | **Cámbiala en producción** |
| `DATABASE_URL` | *(vacía → SQLite)* | Si existe, usa PostgreSQL (Render) |
| `MS_TIMEOUT_LECTURA` | `15` | Segundos de espera por servicio al leer |
| `MS_TIMEOUT_ESCRITURA` | `45` | Segundos de espera por servicio al escribir |
| `MS_PLAZO_TOTAL` | `27` | Tope total (segundos) para probar los 4 servicios en una sola petición |
| `DESACTIVAR_VIGILANTE` | — | Con `1` apaga la sincronización automática en segundo plano |

## ☁️ Desplegar tu propia copia (Render)

1. **Django:** *Web Service* con carpeta raíz `finanzas`.
   Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
   Start: `gunicorn mysite.wsgi`
   Variables: `DATABASE_URL` (PostgreSQL de Render), `SECRET_KEY` y las `MS_*_URL`.
2. **Microservicios:** un *Web Service* por cada uno (`microservicio`, `microservicio-node`, `microservicio-go`; el de Java con Docker) con `SUPABASE_URL`, `SUPABASE_KEY` y `MISTRAL_API_KEY`.
3. Pega las URLs de los microservicios en las variables `MS_*_URL` de Django.

> 💤 **Plan gratis de Render:** los servicios se apagan tras ~15 min sin uso. Entra a la pestaña **Microservicios** y pulsa *Encender todos*; en cuanto uno responda, lo pendiente se sincroniza solo con Supabase.

## 🛠️ Tecnologías

Django · Python/Flask · Java · Node.js · Go · Supabase (PostgREST) · PostgreSQL · SQLite · Mistral AI · Bootstrap 5 · Render

## 📄 Licencia

Proyecto académico de la Facultad de Ingeniería (UNINPAHU). Puedes usarlo, modificarlo y adaptarlo a tu gusto.
