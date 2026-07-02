# -*- coding: utf-8 -*-
"""
Lógica de descarga y extracción de licencias médicas desde lmempleador.cl.

Este módulo es el mismo motor que la versión de Colab, adaptado para correr
en Streamlit Cloud:
- Usa Chromium (instalado vía packages.txt) en vez de Google Chrome.
- Usa carpetas temporales en vez de rutas fijas de Colab (/content/...).
- El logging se puede redirigir a un callback (para mostrarlo en vivo en la
  interfaz de Streamlit) en vez de solo a la consola.
- Expone una única función de alto nivel `ejecutar_scraping(...)` que hace
  todo el proceso y devuelve las rutas de los resultados.
"""

import os
import re
import time
import glob
import shutil
import logging
import tempfile
from urllib.parse import urljoin, unquote
from datetime import datetime
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

import pandas as pd
import pdfplumber

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException,
    StaleElementReferenceException,
)

URL = "https://www.lmempleador.cl/licenses"

ZONA_CHILE = ZoneInfo("America/Santiago")


def hoy_chile():
    """Fecha/hora actual en Chile (no la del servidor, que en Streamlit
    Cloud corre en UTC y puede estar varias horas adelantada)."""
    return datetime.now(ZONA_CHILE)

# Rutas típicas donde quedan Chromium y su driver al instalarlos vía
# packages.txt en Streamlit Cloud (Debian). Si en tu entorno quedan en otra
# ruta, ajusta estas dos constantes.
CHROMIUM_BIN_CANDIDATOS = ["/usr/bin/chromium", "/usr/bin/chromium-browser"]
CHROMEDRIVER_CANDIDATOS = ["/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"]


def _ubicar_binario(candidatos):
    for c in candidatos:
        if os.path.exists(c):
            return c
    return None


class CallbackLogHandler(logging.Handler):
    """Handler de logging que además de loguear a consola, llama a un
    callback (ej. para ir mostrando el progreso en la interfaz de Streamlit)."""

    def __init__(self, callback=None):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        msg = self.format(record)
        if self.callback:
            try:
                self.callback(msg, record.levelname)
            except Exception:
                pass


def crear_logger(log_callback=None):
    logger = logging.getLogger(f"licencias_{id(log_callback)}")
    logger.setLevel(logging.INFO)
    logger.handlers = []  # evita handlers duplicados si se llama varias veces
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    handler_consola = logging.StreamHandler()
    handler_consola.setFormatter(formatter)
    logger.addHandler(handler_consola)

    if log_callback:
        handler_cb = CallbackLogHandler(log_callback)
        handler_cb.setFormatter(formatter)
        logger.addHandler(handler_cb)

    return logger


# ------------------------------------------------------------------
# DEBUG
# ------------------------------------------------------------------
def guardar_debug(driver, carpeta_debug, contador, nombre, log):
    contador["n"] += 1
    prefijo = f"{contador['n']:02d}_{nombre}"
    try:
        driver.save_screenshot(os.path.join(carpeta_debug, f"{prefijo}.png"))
        with open(os.path.join(carpeta_debug, f"{prefijo}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log.info(f"[debug] guardado {prefijo}.png / .html  (url actual: {driver.current_url})")
    except Exception as e:
        log.warning(f"[debug] no se pudo guardar debug '{nombre}': {e}")


# ------------------------------------------------------------------
# DRIVER
# ------------------------------------------------------------------
def iniciar_driver(carpeta_pdf):
    chromium_bin = _ubicar_binario(CHROMIUM_BIN_CANDIDATOS)
    chromedriver_bin = _ubicar_binario(CHROMEDRIVER_CANDIDATOS)

    if not chromium_bin:
        raise RuntimeError(
            "No se encontró Chromium instalado. En Streamlit Cloud, agrega un archivo "
            "'packages.txt' en la raíz del repo con las líneas:\n  chromium\n  chromium-driver"
        )

    options = Options()
    options.binary_location = chromium_bin

    prefs = {
        "download.default_directory": carpeta_pdf,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    }

    options.add_experimental_option("prefs", prefs)
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=es-CL")

    if chromedriver_bin:
        service = Service(executable_path=chromedriver_bin)
        driver = webdriver.Chrome(service=service, options=options)
    else:
        # último recurso: dejar que Selenium Manager intente resolverlo solo
        driver = webdriver.Chrome(options=options)

    driver.execute_cdp_cmd("Page.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": carpeta_pdf,
    })

    return driver


# ------------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------------
def cerrar_banners_cookies(driver):
    posibles = [
        "//*[contains(text(),'Aceptar')]",
        "//*[contains(text(),'Acepto')]",
        "//*[contains(text(),'Entendido')]",
        "//button[contains(@class,'cookie')]",
    ]
    for xp in posibles:
        try:
            el = driver.find_element(By.XPATH, xp)
            if el.is_displayed():
                el.click()
                time.sleep(1)
                return
        except Exception:
            continue


TEXTOS_BOTON_LOGIN = [
    "Siguiente", "Ingresar", "Continuar", "Iniciar sesión", "Iniciar Sesión",
    "Acceder", "Entrar", "Enviar", "Login", "Sign in", "Aceptar",
]


def click_boton_envio_login(driver, wait):
    for texto in TEXTOS_BOTON_LOGIN:
        try:
            el = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable(
                    (By.XPATH, f"//*[self::button or self::a or self::span or self::div]"
                                f"[contains(normalize-space(text()),'{texto}')]")
                )
            )
            el.click()
            return True, texto
        except TimeoutException:
            continue
        except Exception:
            continue
    try:
        el = driver.find_element(By.XPATH, "//button[@type='submit'] | //input[@type='submit']")
        el.click()
        return True, "type=submit"
    except NoSuchElementException:
        pass
    return False, None


def listar_botones_en_pantalla(driver, log):
    log.info("=== Botones/enlaces visibles en esta pantalla (para diagnóstico) ===")
    elementos = driver.find_elements(
        By.XPATH, "//button | //a | //input[@type='submit' or @type='button'] | //*[@role='button']"
    )
    for i, el in enumerate(elementos, start=1):
        try:
            if not el.is_displayed():
                continue
            texto = el.text.strip() or el.get_attribute("value") or el.get_attribute("aria-label") or ""
            log.info(f"  [{i}] <{el.tag_name}> texto='{texto}'")
        except Exception:
            continue


def login(driver, email, password, carpeta_debug, contador_debug, log):
    wait = WebDriverWait(driver, 40)

    driver.get(URL)
    time.sleep(4)
    guardar_debug(driver, carpeta_debug, contador_debug, "pagina_inicial", log)
    cerrar_banners_cookies(driver)

    try:
        wait.until(
            EC.element_to_be_clickable((By.XPATH, "//*[contains(text(),'Clave PortalUE')]"))
        ).click()
    except TimeoutException:
        guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_no_encontro_boton_clave_portalue", log)
        raise RuntimeError("No se encontró el botón 'Clave PortalUE' en la página inicial.")

    time.sleep(3)
    guardar_debug(driver, carpeta_debug, contador_debug, "despues_click_clave_portalue", log)

    try:
        correo = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='email' or @type='text']")))
        correo.clear()
        correo.send_keys(email)

        clave = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='password']")))
        clave.clear()
        clave.send_keys(password)
    except TimeoutException:
        guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_no_encontro_campos_login", log)
        raise RuntimeError("No se encontraron los campos de correo/clave.")

    guardar_debug(driver, carpeta_debug, contador_debug, "credenciales_ingresadas", log)

    exito, texto_usado = click_boton_envio_login(driver, wait)
    if not exito:
        guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_no_encontro_boton_siguiente", log)
        listar_botones_en_pantalla(driver, log)
        raise RuntimeError("No se encontró el botón para enviar el login con ninguno de los textos probados.")

    log.info(f"Botón de login encontrado y clickeado con texto: '{texto_usado}'")
    time.sleep(8)
    guardar_debug(driver, carpeta_debug, contador_debug, "despues_login", log)


# ------------------------------------------------------------------
# SELECCIÓN DE HOSPITAL Y FILTRO DE FECHAS
# ------------------------------------------------------------------
def seleccionar_hospital(driver, wait, nombre_hospital):
    try:
        selects = driver.find_elements(By.XPATH, "//select")
        for sel_el in selects:
            sel = Select(sel_el)
            textos = [o.text.strip().upper() for o in sel.options]
            if any(nombre_hospital in t for t in textos):
                sel.select_by_visible_text(
                    next(o.text for o in sel.options if nombre_hospital in o.text.upper())
                )
                return True
    except Exception:
        pass

    try:
        posibles_triggers = driver.find_elements(
            By.XPATH,
            "//*[contains(@class,'select') or contains(@role,'combobox') or contains(@class,'dropdown')]"
        )
        for trigger in posibles_triggers:
            try:
                if not trigger.is_displayed():
                    continue
                trigger.click()
                time.sleep(1)
                opcion = wait.until(
                    EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(),'{nombre_hospital}')]"))
                )
                opcion.click()
                return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def seleccionar_hospital_y_filtrar(driver, nombre_hospital, carpeta_debug, contador_debug, log):
    wait = WebDriverWait(driver, 40)

    hoy = hoy_chile()
    desde = (hoy - relativedelta(months=1)).strftime("%d/%m/%Y")
    hasta = hoy.strftime("%d/%m/%Y")  # rango móvil: un mes hacia atrás desde hoy

    time.sleep(5)
    guardar_debug(driver, carpeta_debug, contador_debug, "antes_seleccionar_hospital", log)

    if not seleccionar_hospital(driver, wait, nombre_hospital):
        guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_no_selecciono_hospital", log)
        log.warning("No se pudo seleccionar el hospital automáticamente (puede que ya venga preseleccionado).")

    time.sleep(2)

    inputs = driver.find_elements(By.XPATH, "//input")
    campos_fecha = []
    for inp in inputs:
        valor = inp.get_attribute("value") or ""
        placeholder = inp.get_attribute("placeholder") or ""
        tipo = inp.get_attribute("type") or ""
        if tipo == "date" or "/" in valor or "fecha" in placeholder.lower() or "dd" in placeholder.lower():
            campos_fecha.append(inp)

    log.info(f"Campos de fecha candidatos encontrados: {len(campos_fecha)}")

    if len(campos_fecha) >= 2:
        for campo_el, valor in ((campos_fecha[-2], desde), (campos_fecha[-1], hasta)):
            try:
                campo_el.click()
                campo_el.send_keys(Keys.CONTROL, "a")
                campo_el.send_keys(Keys.DELETE)
                campo_el.send_keys(valor)
                driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                    campo_el,
                )
            except ElementClickInterceptedException:
                log.warning("Un campo de fecha estaba tapado por otro elemento (posible overlay).")
    else:
        guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_no_encontro_campos_fecha", log)
        log.warning("No se encontraron 2 campos de fecha.")

    guardar_debug(driver, carpeta_debug, contador_debug, "despues_llenar_fechas", log)

    botones_filtrar = driver.find_elements(
        By.XPATH, "//button[contains(translate(text(),'FILTRARBUSCAR','filtrarbuscar'),'filtrar') "
                  "or contains(translate(text(),'FILTRARBUSCAR','filtrarbuscar'),'buscar')]"
    )

    if botones_filtrar:
        driver.execute_script("arguments[0].scrollIntoView(true);", botones_filtrar[-1])
        time.sleep(1)
        botones_filtrar[-1].click()
        time.sleep(6)
        log.info(f"Filtro aplicado: {desde} hasta {hasta}")
    else:
        guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_no_encontro_boton_filtrar", log)
        log.warning("No se encontró un botón 'Filtrar'/'Buscar'.")

    guardar_debug(driver, carpeta_debug, contador_debug, "despues_filtrar", log)


# ------------------------------------------------------------------
# DESCARGA DE PDFs
# ------------------------------------------------------------------
def arreglar_texto(s):
    if not s:
        return s
    if "%" in s:
        try:
            s = unquote(s)
        except Exception:
            pass
    try:
        arreglado = s.encode("latin-1").decode("utf-8")
        if "Ã" not in arreglado and "\ufffd" not in arreglado:
            return arreglado
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return s


def parsear_fila_tabla(texto_fila):
    lineas = [l.strip() for l in texto_fila.split("\n") if l.strip()]
    datos = {
        "Folio": lineas[0] if lineas else "",
        "Estado Código": "", "Estado Descripción": "", "Trabajador": "",
        "Rut Trabajador": "", "Reposo Total": "", "Fecha Emisión": "",
        "Tipo": "", "Operador": "",
    }
    idx_estado = next((i for i, l in enumerate(lineas) if re.match(r"Estado\s*\d+", l)), None)
    idx_rut = next((i for i, l in enumerate(lineas) if l.upper().startswith("RUT")), None)

    if idx_estado is not None:
        m = re.search(r"Estado\s*(\d+)", lineas[idx_estado])
        datos["Estado Código"] = m.group(1) if m else ""

    if idx_estado is not None and idx_rut is not None and idx_rut > idx_estado:
        bloque = lineas[idx_estado + 1: idx_rut]
        desc_lineas, nombre_lineas = [], []
        for l in bloque:
            if l == l.upper() and any(c.isalpha() for c in l):
                nombre_lineas.append(l)
            else:
                desc_lineas.append(l)
        datos["Estado Descripción"] = arreglar_texto(" ".join(desc_lineas).strip())
        datos["Trabajador"] = arreglar_texto(" ".join(nombre_lineas).strip())

    if idx_rut is not None:
        m = re.search(r"Rut\s*:\s*([\dKk\.\-]+)", lineas[idx_rut])
        datos["Rut Trabajador"] = m.group(1) if m else ""

    m = re.search(r"Reposo Total:\s*([^\n]+)", texto_fila)
    if m: datos["Reposo Total"] = m.group(1).strip()
    m = re.search(r"Fecha de emisi[oó]n:\s*([\d/]+)", texto_fila, re.IGNORECASE)
    if m: datos["Fecha Emisión"] = m.group(1).strip()
    m = re.search(r"Tipo:\s*([^\n]+)", texto_fila)
    if m: datos["Tipo"] = arreglar_texto(m.group(1).strip())
    m = re.search(r"Operador:\s*([^\n]+)", texto_fila)
    if m: datos["Operador"] = m.group(1).strip()

    return datos


def listar_archivos(carpeta):
    return set(os.listdir(carpeta))


def esperar_nuevo_archivo(carpeta, archivos_antes, timeout=40):
    fin = time.time() + timeout
    while time.time() < fin:
        actuales = listar_archivos(carpeta)
        nuevos = actuales - archivos_antes
        nuevos_completos = [n for n in nuevos if not n.endswith(".crdownload")]
        if nuevos_completos:
            time.sleep(0.5)
            return os.path.join(carpeta, nuevos_completos[0])
        time.sleep(0.5)
    return None


def esperar_pagina_tranquila(driver, timeout=10):
    selectores_spinner = [
        "//*[contains(@class,'spinner')]", "//*[contains(@class,'loading')]",
        "//*[contains(@class,'loader')]", "//*[contains(@class,'overlay')]",
    ]
    fin = time.time() + timeout
    while time.time() < fin:
        visible = False
        for xp in selectores_spinner:
            try:
                els = driver.find_elements(By.XPATH, xp)
                if any(e.is_displayed() for e in els):
                    visible = True
                    break
            except Exception:
                continue
        if not visible:
            return
        time.sleep(0.3)


def obtener_filas(driver):
    return driver.find_elements(By.XPATH, "//table//tbody//tr | //tr[contains(.,'PDF')]")


def extraer_folio_del_contenido_pdf(ruta_pdf, log):
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = pdf.pages[0].extract_text() or ""
        texto = quitar_marca_agua(texto)
        m = re.search(r"N[°º]\s*\d\s+([\dKk\-]{6,})", texto)
        return m.group(1) if m else ""
    except Exception as e:
        log.warning(f"No se pudo verificar el folio dentro de {os.path.basename(ruta_pdf)}: {e}")
        return ""


def intentar_descargar_fila(driver, idx, total_filas, carpeta_pdf, log, intentos=3):
    for intento in range(1, intentos + 1):
        try:
            esperar_pagina_tranquila(driver, timeout=8)

            filas = obtener_filas(driver)
            if idx >= len(filas):
                log.warning(f"Fila {idx + 1}: ya no existe en el DOM (intento {intento}/{intentos}).")
                time.sleep(1)
                continue
            fila = filas[idx]

            texto_fila = fila.text
            datos_fila = parsear_fila_tabla(texto_fila)

            if "PDF" not in texto_fila.upper():
                return False, None, datos_fila

            folio_match = re.search(r"([0-9]{6,}-[0-9Kk])", texto_fila)
            nombre_base = folio_match.group(1) if folio_match else f"licencia_fila_{idx + 1}_{int(time.time())}"

            boton_pdf = fila.find_element(By.XPATH, ".//*[contains(text(),'PDF')]")
            archivos_antes = listar_archivos(carpeta_pdf)

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", boton_pdf)
            time.sleep(0.4)

            try:
                boton_pdf.click()
            except (ElementClickInterceptedException, StaleElementReferenceException):
                driver.execute_script("arguments[0].click();", boton_pdf)

            ruta_descargada = esperar_nuevo_archivo(carpeta_pdf, archivos_antes, timeout=45)

            if not ruta_descargada:
                ventanas = driver.window_handles
                if len(ventanas) > 1:
                    driver.switch_to.window(ventanas[-1])
                    time.sleep(2)
                    archivos_antes2 = listar_archivos(carpeta_pdf)
                    ruta_descargada = esperar_nuevo_archivo(carpeta_pdf, archivos_antes2, timeout=20)
                    driver.close()
                    driver.switch_to.window(ventanas[0])

            if ruta_descargada:
                nueva_ruta = os.path.join(carpeta_pdf, f"{nombre_base}.pdf")
                contador = 1
                while os.path.exists(nueva_ruta):
                    nueva_ruta = os.path.join(carpeta_pdf, f"{nombre_base}_{contador}.pdf")
                    contador += 1
                if ruta_descargada != nueva_ruta:
                    shutil.move(ruta_descargada, nueva_ruta)

                folio_real = extraer_folio_del_contenido_pdf(nueva_ruta, log)
                if folio_real and folio_real != nombre_base:
                    log.warning(
                        f"⚠️ DESAJUSTE fila {idx + 1}: se esperaba folio '{nombre_base}' "
                        f"pero el PDF contiene '{folio_real}'. Se corrige y se reintenta."
                    )
                    ruta_correcta = os.path.join(carpeta_pdf, f"{folio_real}.pdf")
                    contador = 1
                    while os.path.exists(ruta_correcta):
                        ruta_correcta = os.path.join(carpeta_pdf, f"{folio_real}_{contador}.pdf")
                        contador += 1
                    shutil.move(nueva_ruta, ruta_correcta)
                    time.sleep(1.5)
                    continue

                log.info(f"Descargado ({idx + 1}/{total_filas}, intento {intento}): {os.path.basename(nueva_ruta)}")
                datos_fila["Archivo PDF"] = os.path.basename(nueva_ruta)
                time.sleep(1)
                return True, nombre_base, datos_fila

            log.warning(f"Fila {idx + 1} ({nombre_base}): no se detectó descarga en el intento {intento}/{intentos}.")
            time.sleep(1.5)

        except StaleElementReferenceException:
            log.warning(f"Fila {idx + 1}: referencia obsoleta (intento {intento}/{intentos}).")
            time.sleep(1.5)
        except Exception as e:
            log.warning(f"Fila {idx + 1}: error en intento {intento}/{intentos}: {e}")
            time.sleep(1.5)

    return False, None, {}


def descargar_pdfs_pagina(driver, carpeta_pdf, log):
    total_filas = len(obtener_filas(driver))
    log.info(f"Filas encontradas: {total_filas}")

    if total_filas == 0:
        log.warning("No se encontró ninguna fila de resultados en esta página.")
        return 0, [], []

    total_descargados = 0
    fallidos = []
    registros = []

    for idx in range(total_filas):
        exito, nombre_base, datos_fila = intentar_descargar_fila(driver, idx, total_filas, carpeta_pdf, log, intentos=3)
        if datos_fila:
            registros.append(datos_fila)
        if exito:
            total_descargados += 1
        else:
            filas_actuales = obtener_filas(driver)
            if idx < len(filas_actuales) and "PDF" in filas_actuales[idx].text.upper():
                fallidos.append(idx + 1)

    if fallidos:
        log.warning(f"Filas que NO se pudieron descargar en esta página tras 3 intentos: {fallidos}")

    log.info(f"PDF descargados en esta página: {total_descargados}/{total_filas} | registros: {len(registros)}")
    return total_descargados, fallidos, registros


def obtener_folios_actuales(driver):
    folios = []
    for fila in obtener_filas(driver):
        try:
            m = re.search(r"([0-9]{6,}-[0-9Kk])", fila.text)
            if m:
                folios.append(m.group(1))
        except StaleElementReferenceException:
            continue
    return folios


def obtener_total_registros_esperado(driver, intentos=4, espera=1.5):
    """Lee 'Mostrando registros... de un total de N registros'. Reintenta
    varias veces y se queda con el valor MÁXIMO visto, porque justo después
    de aplicar un filtro amplio (ej. 1 mes) la tabla puede mostrar primero un
    número parcial mientras termina de cargar de forma asíncrona."""
    mejor = None
    for _ in range(intentos):
        try:
            elementos = driver.find_elements(By.XPATH, "//*[contains(text(),'total de')]")
            for el in elementos:
                m = re.search(r"total de\s+(\d+)", el.text)
                if m:
                    valor = int(m.group(1))
                    if mejor is None or valor > mejor:
                        mejor = valor
        except Exception:
            pass
        time.sleep(espera)
    return mejor


def _intentar_click_pagina_numerada(driver, siguiente_pagina_num, log):
    try:
        candidatos = driver.find_elements(
            By.XPATH,
            f"//*[self::button or self::a or self::li or self::span]"
            f"[normalize-space(text())='{siguiente_pagina_num}']"
        )
        candidatos = [c for c in candidatos if c.is_displayed()]
        if not candidatos:
            return False
        if len(candidatos) > 1:
            log.info(
                f"Aviso: se encontraron {len(candidatos)} elementos visibles con el "
                f"texto '{siguiente_pagina_num}' (posible layout duplicado). Se usa el último."
            )
        boton_num = candidatos[-1]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", boton_num)
        time.sleep(0.5)
        boton_num.click()
        return True
    except Exception:
        return False


def _intentar_click_siguiente(driver, log):
    for texto in ["Siguiente", "Próxima", "Próximo", "Next", ">"]:
        try:
            candidatos = driver.find_elements(By.XPATH, f"//*[normalize-space(text())='{texto}']")
            candidatos = [c for c in candidatos if c.is_displayed()]
            if not candidatos:
                continue
            siguiente = candidatos[-1]
            clase = siguiente.get_attribute("class") or ""
            aria = siguiente.get_attribute("aria-disabled") or ""
            disabled_attr = siguiente.get_attribute("disabled")
            if "disabled" in clase.lower() or aria.lower() == "true" or disabled_attr:
                continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", siguiente)
            time.sleep(0.5)
            siguiente.click()
            return True
        except Exception:
            continue
    return False


def _esperar_cambio_folios(driver, folios_antes, timeout):
    fin = time.time() + timeout
    while time.time() < fin:
        time.sleep(0.7)
        folios_actuales = obtener_folios_actuales(driver)
        if folios_actuales and folios_actuales != folios_antes:
            return True
    return False


def ir_siguiente_pagina(driver, pagina_actual, folios_antes, log, timeout_espera=20):
    """Avanza de página probando, EN ORDEN, hasta 2 estrategias distintas:
    1) clic en el número de página siguiente, 2) clic en 'Siguiente'/equivalentes.
    Si la primera 'tuvo éxito' (encontró y clickeó algo) pero los folios NO
    cambiaron de verdad (ej. porque clickeó un elemento oculto/duplicado),
    se prueba también la segunda estrategia antes de rendirse."""
    siguiente_pagina_num = pagina_actual + 1

    if _intentar_click_pagina_numerada(driver, siguiente_pagina_num, log):
        if _esperar_cambio_folios(driver, folios_antes, timeout_espera):
            return True
        log.warning(
            f"Se clickeó el número de página {siguiente_pagina_num} pero los folios no "
            f"cambiaron. Probando con el botón 'Siguiente' como respaldo..."
        )

    if _intentar_click_siguiente(driver, log):
        if _esperar_cambio_folios(driver, folios_antes, timeout_espera):
            return True
        log.warning(f"Se clickeó 'Siguiente' pero los folios tampoco cambiaron.")

    return False


def descargar_todas_las_paginas(driver, carpeta_pdf, log, progreso_callback=None):
    pagina = 1
    total_general = 0
    fallidos_general = []
    registros_globales = []

    total_esperado = obtener_total_registros_esperado(driver)
    if total_esperado:
        log.info(f"Total de registros esperados según la página: {total_esperado}")

    while True:
        log.info(f"--- Página {pagina} ---")
        folios_antes = obtener_folios_actuales(driver)
        descargados, fallidos, registros = descargar_pdfs_pagina(driver, carpeta_pdf, log)
        total_general += descargados
        fallidos_general.extend([(pagina, idx) for idx in fallidos])
        registros_globales.extend(registros)

        if progreso_callback:
            progreso_callback(total_general, total_esperado)

        if not ir_siguiente_pagina(driver, pagina, folios_antes, log):
            log.info("No hay más páginas.")
            break
        pagina += 1

    log.info(f"TOTAL de PDFs descargados: {total_general}")
    log.info(f"TOTAL de registros de tabla capturados: {len(registros_globales)}")
    if total_esperado and total_general < total_esperado:
        log.warning(
            f"⚠️ Se esperaban {total_esperado} registros pero solo se descargaron "
            f"{total_general}. Es posible que la paginación se haya cortado antes de "
            f"tiempo. Revisa si en el sitio realmente hay más páginas de las procesadas."
        )
    if fallidos_general:
        log.warning(f"Filas fallidas (página, fila): {fallidos_general}")
    return total_general, registros_globales


# ------------------------------------------------------------------
# EXTRACCIÓN DE DATOS DESDE LOS PDF
# ------------------------------------------------------------------
CAMPOS_COMPROBANTE = [
    (r"Sucursal", "Sucursal"),
    (r"Fecha Otorgamiento", "Fecha Otorgamiento"),
    (r"Entidad que se pronuncia", "Entidad que se pronuncia"),
    (r"Empleador", "Empleador"),
    (r"Profesional", "Profesional"),
    (r"Rut", "Rut Profesional"),
    (r"Especialidad", "Especialidad"),
    (r"Direcci[oó]n", "Dirección Profesional"),
    (r"Nombre", "Nombre Trabajador"),
    (r"Rut", "Rut Trabajador"),
    (r"Edad", "Edad"),
    (r"Sexo", "Sexo"),
    (r"Tipo Licencia", "Tipo Licencia"),
    (r"Fecha Inicio", "Fecha Inicio Reposo"),
    (r"Lugar", "Lugar Reposo"),
    (r"N[°ºo]\s*D[ií]as", "N° Días"),
    (r"Direcci[oó]n", "Dirección Reposo"),
    (r"\bFecha\s*:", None),
    (r"Tel[eé]fono", "Teléfono"),
    (r"\bt[eé]rmino\b", None),
    (r"Tipo", "Tipo Reposo"),
    (r"Estado", "Estado"),
    (r"Fecha [UÚ]ltima Modificaci[oó]n", "Fecha Última Modificación"),
    (r"Motivo Anulaci[oó]n", "Motivo Anulación"),
    (r"Motivo Rechazo", "Motivo Rechazo"),
    (r"Motivo Devoluci[oó]n", "Motivo Devolución"),
    (r"Rut del Empleador", "Rut Empleador"),
    (r"Fecha de Recepci[oó]n", "Fecha Recepción"),
    (r"Fecha de env[ií]o a pronunciamiento", "Fecha Envío Pronunciamiento"),
    (r"(?:No hay pronunciamientos|Puede revisar el estado|Folio\s*:)", None),
]


def quitar_marca_agua(texto):
    return re.sub(r"(?<!\S)[A-ZÁÉÍÓÚÑ](?!\S)", " ", texto)


def extraer_fecha_termino(texto):
    m = re.search(r"\bFecha\s*:\s*([\d]{1,2}[-/][\d]{1,2}[-/][\d]{2,4})", texto)
    return m.group(1) if m else ""


def extraer_folio_de_nombre_archivo(ruta_pdf):
    nombre = os.path.basename(ruta_pdf)
    m = re.match(r"([0-9]{6,}-[0-9Kk])", nombre)
    return m.group(1) if m else ""


def limpiar_valor_comprobante(v):
    v = re.sub(
        r"\d+\.\s*Datos\s*(Profesional|Trabajador|Reposo|del Empleador|de pronunciamiento)?",
        " ", v, flags=re.IGNORECASE,
    )
    v = re.sub(r"\d+\.\s*Estado de la licencia", " ", v, flags=re.IGNORECASE)
    v = re.sub(r"N[°º]\s*\d\s+[\dKk\-]{6,}", " ", v)
    v = re.sub(r"\s*\d+\.\s*$", "", v)
    v = re.sub(r"\s+", " ", v).strip(" .:")
    return v


def extraer_texto_comprobante(ruta_pdf):
    with pdfplumber.open(ruta_pdf) as pdf:
        texto_ultima = pdf.pages[-1].extract_text() or ""
        if "Comprobante de Licencia" in texto_ultima or "Sucursal" in texto_ultima:
            return texto_ultima
        for pagina in pdf.pages:
            t = pagina.extract_text() or ""
            if "Comprobante de Licencia" in t:
                return t
        texto_completo = ""
        for pagina in pdf.pages:
            texto_completo += (pagina.extract_text() or "") + "\n"
        return texto_completo


def extraer_campos_comprobante(texto):
    texto_norm = re.sub(r"\s+", " ", texto)
    resultados = {}
    pos = 0
    for i, (patron_label, nombre_amigable) in enumerate(CAMPOS_COMPROBANTE):
        if nombre_amigable is None:
            continue
        m = re.search(patron_label + r"\s*:\s*", texto_norm[pos:], re.IGNORECASE)
        if not m:
            resultados[nombre_amigable] = ""
            continue
        inicio_valor = pos + m.end()
        fin_valor = len(texto_norm)
        for patron_siguiente, _ in CAMPOS_COMPROBANTE[i + 1:]:
            m2 = re.search(patron_siguiente + r"\s*:?", texto_norm[inicio_valor:], re.IGNORECASE)
            if m2:
                candidato = inicio_valor + m2.start()
                if candidato < fin_valor:
                    fin_valor = candidato
        valor = limpiar_valor_comprobante(texto_norm[inicio_valor:fin_valor])
        resultados[nombre_amigable] = valor
        pos = inicio_valor
    return resultados


def extraer_datos_pdf(ruta_pdf, log):
    texto_crudo = extraer_texto_comprobante(ruta_pdf)
    texto = quitar_marca_agua(texto_crudo)

    datos = {"Archivo PDF": os.path.basename(ruta_pdf), "Ruta PDF": ruta_pdf}
    datos.update(extraer_campos_comprobante(texto))
    datos["Fecha Término"] = extraer_fecha_termino(texto)

    folio_archivo = extraer_folio_de_nombre_archivo(ruta_pdf)
    if folio_archivo:
        datos["Folio"] = folio_archivo
    else:
        folio_header = re.search(r"N[°º]\s*\d\s+([\dKk\-]+)", texto)
        datos["Folio"] = folio_header.group(1) if folio_header else ""

    codigo = re.search(r"c[oó]digo de verificaci[oó]n:?\s*([A-Za-z0-9\-]+)", texto, re.IGNORECASE)
    datos["Código Verificación"] = codigo.group(1) if codigo else ""

    return datos


def verificar_todos_los_pdfs(carpeta_pdf, log):
    pdfs = sorted(glob.glob(f"{carpeta_pdf}/*.pdf"))
    desajustes = []
    for ruta in pdfs:
        nombre_archivo = os.path.basename(ruta)
        folio_nombre = extraer_folio_de_nombre_archivo(ruta)
        folio_real = extraer_folio_del_contenido_pdf(ruta, log)
        if folio_nombre and folio_real and folio_nombre != folio_real:
            desajustes.append((nombre_archivo, folio_nombre, folio_real))

    if desajustes:
        log.warning(f"⚠️ {len(desajustes)} PDF con nombre que no coincide con su contenido.")
        for nombre_archivo, folio_nombre, folio_real in desajustes:
            log.warning(f"   - {nombre_archivo}: nombre '{folio_nombre}' vs contenido '{folio_real}'")
    else:
        log.info(f"Verificación de folios: los {len(pdfs)} PDF coinciden con su nombre de archivo. ✔")

    return desajustes


def generar_excel_desde_tabla(registros, ruta_salida, log):
    columnas = [
        "Folio", "Estado Código", "Estado Descripción", "Trabajador",
        "Rut Trabajador", "Reposo Total", "Fecha Emisión", "Tipo",
        "Operador", "Archivo PDF",
    ]
    df = pd.DataFrame(registros)
    for col in columnas:
        if col not in df.columns:
            df[col] = ""
    df = df[columnas]
    df.to_excel(ruta_salida, index=False)
    log.info(f"Excel (desde tabla web) generado con {len(df)} registros: {ruta_salida}")
    return df


def generar_excel_desde_pdfs(carpeta_pdf, ruta_salida, log):
    registros = []
    pdfs = sorted(glob.glob(f"{carpeta_pdf}/*.pdf"))
    log.info(f"PDF encontrados para procesar: {len(pdfs)}")

    for ruta in pdfs:
        try:
            registros.append(extraer_datos_pdf(ruta, log))
        except Exception as e:
            log.warning(f"Error procesando {ruta}: {e}")

    df = pd.DataFrame(registros)
    df.to_excel(ruta_salida, index=False)
    log.info(f"Excel (desde PDFs, completo) generado con {len(df)} registros: {ruta_salida}")
    return df


# ------------------------------------------------------------------
# FUNCIÓN DE ALTO NIVEL (la usa app.py)
# ------------------------------------------------------------------
def ejecutar_scraping(email, password, nombre_hospital, log_callback=None, progreso_callback=None):
    """Ejecuta todo el proceso de principio a fin. Devuelve un dict con las
    rutas de resultados y estadísticas, para que la interfaz de Streamlit
    los muestre y ofrezca para descargar."""
    log = crear_logger(log_callback)

    carpeta_base = tempfile.mkdtemp(prefix="licencias_")
    carpeta_pdf = os.path.join(carpeta_base, "pdfs")
    carpeta_resultados = os.path.join(carpeta_base, "resultados")
    carpeta_debug = os.path.join(carpeta_base, "debug")
    for c in (carpeta_pdf, carpeta_resultados, carpeta_debug):
        os.makedirs(c, exist_ok=True)

    contador_debug = {"n": 0}
    driver = None
    total_pdfs = 0
    registros_tabla = []

    try:
        driver = iniciar_driver(carpeta_pdf)
        login(driver, email, password, carpeta_debug, contador_debug, log)
        seleccionar_hospital_y_filtrar(driver, nombre_hospital, carpeta_debug, contador_debug, log)
        total_pdfs, registros_tabla = descargar_todas_las_paginas(
            driver, carpeta_pdf, log, progreso_callback=progreso_callback
        )
    except Exception as e:
        if driver:
            guardar_debug(driver, carpeta_debug, contador_debug, "ERROR_fatal", log)
        log.error(f"El proceso se detuvo con un error: {e}")
        raise
    finally:
        if driver:
            driver.quit()

    verificar_todos_los_pdfs(carpeta_pdf, log)

    ruta_excel_pdfs = os.path.join(carpeta_resultados, "resumen_licencias.xlsx")
    ruta_excel_tabla = os.path.join(carpeta_resultados, "resumen_licencias_tabla_web.xlsx")
    generar_excel_desde_pdfs(carpeta_pdf, ruta_excel_pdfs, log)
    generar_excel_desde_tabla(registros_tabla, ruta_excel_tabla, log)

    ruta_zip_pdfs = os.path.join(carpeta_base, "licencias_pdf")
    shutil.make_archive(ruta_zip_pdfs, "zip", carpeta_pdf)

    return {
        "total_pdfs": total_pdfs,
        "carpeta_pdf": carpeta_pdf,
        "carpeta_debug": carpeta_debug,
        "ruta_excel_pdfs": ruta_excel_pdfs,
        "ruta_excel_tabla": ruta_excel_tabla,
        "ruta_zip_pdfs": ruta_zip_pdfs + ".zip",
    }
