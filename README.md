# Licencias Médicas SSMOCC — Descarga y extracción automática

App de Streamlit que descarga las licencias médicas del Hospital Félix Bulnes
desde [lmempleador.cl](https://www.lmempleador.cl/licenses) (filtradas por el
día de hoy), extrae todos los datos de cada PDF, y entrega:

- Excel completo con todos los campos de cada licencia (Sucursal, Empleador,
  Profesional, Trabajador, Reposo, Estado, RUT Empleador, fechas, folio, etc.)
- Excel de respaldo con los datos vistos directamente en la tabla web
- ZIP con todos los PDF descargados

## Estructura del proyecto

```
├── app.py                 # Interfaz de Streamlit
├── licencias_scraper.py   # Toda la lógica de scraping/extracción
├── requirements.txt       # Librerías de Python
├── packages.txt           # Dependencias del sistema (Chromium) para Streamlit Cloud
└── .gitignore
```

## 1. Subir a GitHub

```bash
cd licencias-streamlit
git init
git add .
git commit -m "Primera versión: descarga y extracción de licencias médicas"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

Si ya tienes un repo creado en GitHub, reemplaza la URL de `git remote add`
por la tuya.

## 2. Desplegar en Streamlit Community Cloud

1. Entra a [share.streamlit.io](https://share.streamlit.io) con tu cuenta de GitHub.
2. Click en **"New app"**.
3. Elige tu repositorio, la rama (`main`) y el archivo principal (`app.py`).
4. Click en **"Deploy"**.

Streamlit Cloud va a instalar automáticamente lo que está en `requirements.txt`
(librerías Python) y `packages.txt` (Chromium + su driver, vía apt) antes de
levantar la app. La primera vez puede tardar unos minutos.

## 3. Usar la app

Al abrir la app, se pide el correo y clave de PortalUE en un formulario (no se
guardan en ningún lado, solo se usan durante esa ejecución). Al enviar,
empieza el proceso y se ve el progreso en vivo; al terminar aparecen los
botones de descarga del Excel y el ZIP de PDFs.

## Notas importantes

- **Nunca subas tus credenciales al repositorio.** El formulario las pide en
  cada ejecución justamente para evitar tener que guardarlas en el código.
- **Tiempos de espera:** Streamlit Community Cloud tiene límites de recursos
  (CPU/RAM) y puede ser más lento que Colab para correr Chromium headless.
  Si el proceso es muy largo (muchas licencias), puede convenir correrlo por
  partes o localmente.
- **Si el sitio cambia** (textos de botones, estructura de la tabla), revisa
  los mensajes `WARNING`/`ERROR` que se muestran en vivo en la app — ahí se
  detalla en qué paso se atascó.
- Este scraper depende de la estructura actual de lmempleador.cl; si el sitio
  cambia su diseño, puede requerir ajustes en `licencias_scraper.py`.
