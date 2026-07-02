# -*- coding: utf-8 -*-
"""App de Streamlit: descarga y extracción de Licencias Médicas (lmempleador.cl)."""

import streamlit as st
from dateutil.relativedelta import relativedelta

from licencias_scraper import ejecutar_scraping, hoy_chile

st.set_page_config(page_title="Licencias Médicas SSMOCC", page_icon="🩺", layout="centered")

st.title("🩺 Descarga de Licencias Médicas")
_hoy = hoy_chile()
_desde = _hoy - relativedelta(months=1)
st.caption(
    f"Filtro: {_desde.strftime('%d/%m/%Y')} al {_hoy.strftime('%d/%m/%Y')} "
    f"(último mes) — Hospital Félix Bulnes"
)

with st.form("credenciales"):
    email = st.text_input("Correo PortalUE")
    password = st.text_input("Clave PortalUE", type="password")
    hospital = st.text_input(
        "Nombre del hospital (tal como aparece en el selector del sitio)",
        value="HOSPITAL CLINICO DR. FELIX BULNES CERDA",
    )
    enviar = st.form_submit_button("Iniciar descarga")

st.caption(
    "⚠️ Las credenciales no se guardan: se usan solo durante esta ejecución y se pierden "
    "al cerrar o recargar la página."
)

if enviar:
    if not email or not password:
        st.error("Ingresa correo y clave antes de continuar.")
        st.stop()

    log_placeholder = st.empty()
    progreso_placeholder = st.empty()
    barra = st.progress(0)
    lineas_log = []

    def log_callback(mensaje, nivel):
        lineas_log.append(f"{'⚠️ ' if nivel == 'WARNING' else '❌ ' if nivel == 'ERROR' else ''}{mensaje}")
        # muestra las últimas ~40 líneas para no sobrecargar la página
        log_placeholder.code("\n".join(lineas_log[-40:]), language=None)

    def progreso_callback(actual, esperado):
        if esperado:
            barra.progress(min(actual / esperado, 1.0))
            progreso_placeholder.write(f"Progreso: {actual}/{esperado} PDF descargados")
        else:
            progreso_placeholder.write(f"Progreso: {actual} PDF descargados")

    with st.spinner("Procesando... esto puede tardar varios minutos según la cantidad de licencias."):
        try:
            resultado = ejecutar_scraping(
                email=email,
                password=password,
                nombre_hospital=hospital.strip().upper(),
                log_callback=log_callback,
                progreso_callback=progreso_callback,
            )
        except Exception as e:
            st.error(f"El proceso terminó con un error: {e}")
            st.info(
                "Revisa el log de arriba para más detalle. Si el problema persiste, "
                "puede que el sitio haya cambiado algún texto de botón o estructura de página."
            )
            st.stop()

    st.success(f"¡Listo! Se descargaron {resultado['total_pdfs']} PDF.")

    col1, col2, col3 = st.columns(3)
    with col1:
        with open(resultado["ruta_excel_pdfs"], "rb") as f:
            st.download_button(
                "📊 Excel completo (desde PDFs)",
                f,
                file_name="resumen_licencias.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    with col2:
        with open(resultado["ruta_excel_tabla"], "rb") as f:
            st.download_button(
                "📋 Excel de respaldo (tabla web)",
                f,
                file_name="resumen_licencias_tabla_web.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    with col3:
        with open(resultado["ruta_zip_pdfs"], "rb") as f:
            st.download_button(
                "📁 Todos los PDF (.zip)",
                f,
                file_name="licencias_pdf.zip",
                mime="application/zip",
            )
