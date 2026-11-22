# YouTube News Monitor y ScanCards

Proyecto para monitorear videos recientes de canales de YouTube, obtener sus subtítulos o transcribirlos localmente, resumirlos con modelos de lenguaje y producir reportes JSON, Markdown y HTML. Incluye una interfaz web local para configurar y ejecutar el monitoreo.

## Contenido

- [Qué hace](#qué-hace)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Requisitos](#requisitos)
- [Instalación desde GitHub](#instalación-desde-github)
- [Configuración de claves y canales](#configuración-de-claves-y-canales)
- [Uso](#uso)
- [Archivos generados y caché](#archivos-generados-y-caché)
- [Solución de problemas](#solución-de-problemas)
- [Seguridad](#seguridad)

## Qué hace

1. Consulta los canales y las verticales definidas en `config.yaml`.
2. Busca videos recientes y aplica filtros de idioma, duración, transmisiones en vivo y Shorts.
3. Obtiene subtítulos de YouTube; si no están disponibles, puede descargar el audio y transcribirlo con `faster-whisper` en CPU.
4. Genera resúmenes y puntos clave con Gemini. Groq es un proveedor de respaldo opcional; también existe una síntesis extractiva local.
5. Guarda los resultados y genera tarjetas visuales en un reporte HTML.

La aplicación usa YouTube Data API v3. La consulta de canales mediante sus videos subidos ayuda a reducir el consumo de cuota frente a la búsqueda por palabras clave.

## Estructura del proyecto

```text
youtube_monitor/
├── src/
│   ├── app_foresight.py             # Interfaz web local
│   ├── render_youtube_scancards.py  # Exportación de tarjetas y reporte HTML
│   └── youtube_news.py              # Monitoreo, transcripción y resúmenes
├── templates/
│   └── youtube_scancards.html       # Plantilla para el reporte visual
├── cache/                           # Caché SQLite y transcripciones (se crea al ejecutar)
├── output/                          # Reportes y resultados (se crea al ejecutar)
├── .env.example                     # Ejemplo de variables; copiar a .env
├── config.yaml                      # Canales, verticales y rutas
├── requirements.txt                 # Dependencias Python fijadas
└── README.md
```

`cache/` y `output/` son directorios locales ignorados por Git. No es necesario crearlos manualmente.

## Requisitos

- Python 3.10–3.13 de 64 bits. Python 3.13.2 es la versión con la que se verificaron las dependencias fijadas.
- Una clave de YouTube Data API v3.
- Una clave de Gemini para generar resúmenes con Gemini. La síntesis local sigue disponible sin ella; Groq puede configurarse como respaldo.
- Conexión a Internet para consultar YouTube y los servicios de IA.
- Espacio y memoria disponibles para descargar y ejecutar el modelo Whisper. El modelo `small` se usa por defecto y funciona en CPU.

Las versiones directas están fijadas en [`requirements.txt`](requirements.txt). `imageio-ffmpeg` proporciona FFmpeg; no es necesario instalar FFmpeg por separado.

## Instalación desde GitHub

Abre PowerShell, Terminal o una consola en la ubicación donde quieras guardar el proyecto:

```powershell
git clone <URL_DEL_REPOSITORIO>
cd youtube_monitor
```

Reemplaza `<URL_DEL_REPOSITORIO>` con la dirección HTTPS o SSH del repositorio. Los comandos siguientes deben ejecutarse desde la carpeta raíz del proyecto.

### Windows (PowerShell)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Si no tienes Python 3.13, usa una versión de Python entre 3.10 y 3.13 disponible en tu equipo para crear el entorno, por ejemplo `py -3.12 -m venv .venv`.

Si PowerShell impide activar el entorno, puedes permitir scripts locales para tu usuario y volver a activarlo:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### Linux o macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

En Linux, algunas distribuciones requieren herramientas de compilación y bibliotecas de audio del sistema para instalar o ejecutar dependencias de Whisper. Consulta la documentación de tu distribución si la instalación informa que falta un compilador o una biblioteca.

## Configuración de claves y canales

### 1. Crear las claves de API

- **YouTube Data API v3:** crea un proyecto en Google Cloud Console, habilita YouTube Data API v3 y genera una clave de API.
- **Gemini:** crea una clave en Google AI Studio. Es necesaria para usar Gemini.
- **Groq (opcional):** crea una clave de API para habilitar el proveedor de respaldo si Gemini no está disponible.

### 2. Crear el archivo `.env`

En PowerShell:

```powershell
Copy-Item .env.example .env
```

En Linux o macOS:

```bash
cp .env.example .env
```

Edita `.env` y sustituye los valores de ejemplo por tus claves:

```env
YOUTUBE_API_KEY=tu_clave_youtube
GEMINI_API_KEY=tu_clave_gemini
GROQ_API_KEY=tu_clave_groq_opcional
```

`YOUTUBE_API_KEY` es obligatoria. `GEMINI_API_KEY` y `GROQ_API_KEY` son opcionales, pero se recomienda configurar Gemini para obtener resúmenes generados. No agregues comillas ni espacios alrededor de `=`.

### 3. Revisar la configuración

Edita `config.yaml` para cambiar:

- Verticales temáticas, palabras clave y canales en español e inglés.
- Días hacia atrás, máximo de videos por canal y duración máxima.
- Idiomas de subtítulos, exclusión de Shorts y transmisiones en vivo.
- Rutas del caché, resultados JSON y Markdown.

Los argumentos de línea de comandos pueden sobrescribir algunos valores durante una ejecución. La interfaz web carga las verticales y sus palabras clave desde este mismo archivo.

## Uso

Comprueba que el entorno y las dependencias estén listos:

```powershell
python --version
python -m pip check
python src/youtube_news.py --help
python src/render_youtube_scancards.py --help
```

### Ejecutar el monitoreo

Desde la raíz del proyecto:

```powershell
python src/youtube_news.py
```

Al finalizar, los resultados y el reporte Markdown quedan en `output/`.

Ejemplos de opciones:

```powershell
# Limitar a canales por nombre
python src/youtube_news.py --canal "CNN,CNBC"

# Ejecutar una vertical definida en config.yaml
python src/youtube_news.py --vertical fintech

# Cambiar ventana, cantidad por canal y duración máxima
python src/youtube_news.py --dias 7 --max-videos 5 --max-minutos 15

# Buscar por palabras clave en vez de recorrer los canales configurados
python src/youtube_news.py --modo busqueda --keywords "inteligencia artificial" "semiconductores"

# Usar un modelo local de Whisper más pequeño
python src/youtube_news.py --whisper-model base

# Ignorar el caché y volver a procesar los videos encontrados
python src/youtube_news.py --forzar
```

Modelos disponibles para Whisper: `tiny`, `base`, `small` y `medium`. Los modelos más grandes consumen más memoria y tardan más. El modo `busqueda` puede consumir más cuota de YouTube que el modo `canales`.

### Generar las tarjetas y el reporte HTML

Después de ejecutar el monitoreo:

```powershell
python src/render_youtube_scancards.py
```

El renderer usa por defecto `output/resultados.json` y `templates/youtube_scancards.html`, y genera el JSON de tarjetas y `output/reporte_youtube.html`. Para personalizar las rutas:

```powershell
python src/render_youtube_scancards.py `
  --input output/resultados.json `
  --template templates/youtube_scancards.html `
  --json-out output/scancards_youtube.json `
  --html-out output/reporte_youtube.html
```

Abre `output/reporte_youtube.html` en tu navegador.

### Usar la interfaz web

```powershell
python src/app_foresight.py
```

La aplicación abre el navegador en `http://127.0.0.1:8765`. Desde allí puedes elegir vertical, idioma, límites, canales y palabras clave, iniciar el proceso y consultar el reporte. Detén el servidor con `Ctrl+C` en la terminal.

## Archivos generados y caché

- `output/resultados.json`: resultados estructurados del monitoreo.
- `output/resultados.md`: resumen legible en Markdown.
- `output/scancards_youtube.json`: tarjetas en formato JSON.
- `output/reporte_youtube.html`: reporte creado por el renderer independiente.
- `output/reporte_foresight.html`: ubicación del reporte generado desde la interfaz web.
- `cache/youtube_cache.db`: caché SQLite de resultados y resúmenes.
- `cache/transcripts/`: transcripciones locales reutilizables.

El caché evita volver a transcribir y resumir videos ya procesados. Para volver a procesar videos en una ejecución, usa `--forzar`. Para borrar permanentemente los datos del caché, cierra la aplicación y elimina `cache/`; el directorio se recreará en la siguiente ejecución.

## Solución de problemas

- **Falta `YOUTUBE_API_KEY`:** revisa que `.env` exista en la raíz, que contenga la variable y que la clave tenga habilitada YouTube Data API v3.
- **Error de cuota o respuesta `429`:** revisa las cuotas en Google Cloud Console o Google AI Studio, espera a que se restablezcan y considera reducir la cantidad de canales o usar `--modo canales`.
- **No se generó un resumen:** comprueba `GEMINI_API_KEY`; configura `GROQ_API_KEY` si quieres un proveedor de respaldo. Si no se obtiene una transcripción, puede que el video no tenga subtítulos accesibles y Whisper no haya podido transcribirlo.
- **Whisper tarda o consume mucha memoria:** selecciona `--whisper-model tiny` o `base`, reduce la cantidad de videos o aumenta el límite de duración.
- **No se encontró un archivo de entrada o una plantilla:** ejecuta los comandos desde la raíz del proyecto o proporciona las rutas con `--input` y `--template`.
- **Una dependencia no se instala:** confirma la versión de Python con `python --version`, actualiza `pip` e instala las herramientas de compilación que requiera tu sistema operativo.

## Seguridad

No publiques `.env`, claves de API ni datos personales. `.env` se excluye de Git; mantén las claves reales fuera del repositorio y comparte solo `.env.example`. Si una clave se expone, revócala y crea una nueva desde el proveedor correspondiente.
