import streamlit as st
import hmac
import hashlib
import re
import time
import threading
import unicodedata
import numpy as np
import requests
from itertools import permutations
from urllib.parse import urlencode
from supabase import create_client
from rapidocr import RapidOCR
from streamlit_webrtc import webrtc_streamer, WebRtcMode
from PIL import Image, ImageOps

# ---------------------------------------------------------------
# Acceso con contraseña
# ---------------------------------------------------------------
password = st.text_input("🔒 Contraseña de acceso", type="password")

if not hmac.compare_digest(password, st.secrets["APP_PASSWORD"]):
    if password:
        st.error("Contraseña incorrecta")
    st.stop()

# ---------------------------------------------------------------
# Conexiones y configuración
# ---------------------------------------------------------------
supabase = create_client(
    st.secrets["SUPABASE_URL"],
    st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
)

BODEGA_NOMBRE = "Bodega Quilicura"
BODEGA_DIRECCION = "Av. Presidente Eduardo Frei Montalva 8301, Quilicura"
BODEGA_LATITUD = -33.361074
BODEGA_LONGITUD = -70.697689


# ---------------------------------------------------------------
# Funciones
# ---------------------------------------------------------------
@st.cache_resource
def cargar_ocr():
    # Se carga una sola vez (antes se recreaba en cada recarga)
    return RapidOCR()


def _buscar_nominatim(direccion_completa):
    try:
        respuesta_geo = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": direccion_completa,
                "format": "json",
                "limit": 5,
                "countrycodes": "cl"
            },
            headers={"User-Agent": "GaboRutas/1.0 (aplicacion de reparto)"},
            timeout=10
        )
        respuesta_geo.raise_for_status()
        resultados = respuesta_geo.json()
        if resultados:
            return float(resultados[0]["lat"]), float(resultados[0]["lon"])
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None, None


def obtener_coordenadas(direccion, comuna):
    # Prueba varias versiones de la dirección, de la más exacta a la más simple
    limpia = re.sub(r"\b[A-Za-z]{1,2}-?\d+\b", "", direccion)  # quita códigos tipo D-825
    limpia = re.sub(r"\s+", " ", limpia).strip(" ,.-")

    variantes = [f"{direccion}, {comuna}, Chile"]
    if limpia and limpia != direccion:
        variantes.append(f"{limpia}, {comuna}, Chile")
    if limpia:
        variantes.append(f"{limpia}, Chile")

    for i, variante in enumerate(variantes):
        if i > 0:
            time.sleep(1)  # Nominatim pide máximo 1 consulta por segundo
        lat, lon = _buscar_nominatim(variante)
        if lat is not None:
            return lat, lon
    return None, None


def obtener_matriz_rutas(coordenadas):
    puntos = ";".join(f"{lon},{lat}" for lat, lon in coordenadas)
    url = "https://router.project-osrm.org/table/v1/driving/" + puntos
    respuesta = requests.get(
        url,
        params={"annotations": "distance,duration"},
        timeout=20
    )
    respuesta.raise_for_status()
    datos = respuesta.json()
    if datos.get("code") != "Ok":
        raise ValueError("No se pudo calcular la ruta")
    return datos["distances"], datos["durations"]


def optimizar_ruta(distancias):
    cantidad = len(distancias) - 1
    if cantidad == 0:
        return [0, 0], 0

    mejor_orden = None
    menor_distancia = float("inf")

    for orden in permutations(range(1, cantidad + 1)):
        ruta = [0] + list(orden) + [0]
        total = sum(
            distancias[ruta[i]][ruta[i + 1]]
            for i in range(len(ruta) - 1)
        )
        if total < menor_distancia:
            menor_distancia = total
            mejor_orden = ruta

    return mejor_orden, menor_distancia


def normalizar(texto):
    # Quita tildes y pasa a mayúsculas (SEÑOR y SENOR quedan iguales)
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().upper()


def extraer_datos(lineas):
    # Busca la línea "SEÑOR(ES)" y toma nombre, dirección y comuna
    # de las líneas siguientes.
    for i, linea in enumerate(lineas):
        if "SENOR" in normalizar(linea):
            resto = lineas[i + 1:i + 6]
            if len(resto) >= 3:
                nombre = resto[0]
                direccion = resto[1]
                comuna = resto[3] if len(resto) > 3 else resto[2]
                return nombre, direccion, comuna
    return None


# ---------------------------------------------------------------
# Pantalla principal
# ---------------------------------------------------------------
st.title("🚚 GABO RUTAS")
st.write("Bienvenido al planificador de rutas de reparto")

# ---------------------------------------------------------------
# Escanear factura (automático: foto -> OCR -> guardar)
# ---------------------------------------------------------------
st.subheader("📸 Escanear factura en vivo")
st.caption("Enciende la cámara y apunta a la zona «SEÑOR(ES)» de la factura.")

# Estado compartido entre la cámara (otro hilo) y la pantalla
@st.cache_resource
def estado_escaner():
    return {"lock": threading.Lock(), "ocupado": False, "ultimo": 0.0, "datos": None}

estado = estado_escaner()
ocr_en_vivo = cargar_ocr()


def leer_frame(arreglo):
    inicio = time.time()
    try:
        img = Image.fromarray(arreglo)
        img.thumbnail((960, 960))  # más chico = más rápido
        resultado = ocr_en_vivo(np.array(img))
        lineas = [l.strip() for l in (resultado.txts or []) if l.strip()]
        datos = extraer_datos(lineas)
        with estado["lock"]:
            if datos:
                estado["datos"] = datos
            estado["error"] = None
            estado["seg"] = time.time() - inicio
    except Exception as e:
        with estado["lock"]:
            estado["error"] = f"{type(e).__name__}: {e}"
    finally:
        estado["ultimo"] = time.time()  # espera 1 s DESPUÉS de terminar
        estado["ocupado"] = False


def video_frame_callback(frame):
    # Lee un cuadro apenas termina el anterior del video (sin trabar la cámara)
    ahora = time.time()
    if not estado["ocupado"] and ahora - estado["ultimo"] > 1:
        estado["ocupado"] = True
        estado["ultimo"] = ahora
        arreglo = frame.to_ndarray(format="rgb24")
        threading.Thread(target=leer_frame, args=(arreglo,), daemon=True).start()
    return frame


webrtc_streamer(
    key="escaner_facturas",
    mode=WebRtcMode.SENDRECV,
    rtc_configuration={"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]},
    media_stream_constraints={
        "video": {
            "facingMode": "environment",
            "width": {"ideal": 1920},
            "height": {"ideal": 1080},
        },
        "audio": False,
    },
    video_frame_callback=video_frame_callback,
    async_processing=True,
)


@st.fragment(run_every=1)
def panel_deteccion():
    with estado["lock"]:
        datos = estado["datos"]
    if datos:
        nombre_d, dir_d, comuna_d = datos
        st.success(f"**{nombre_d}**\n\n{dir_d} — {comuna_d}")
        if st.button("✨ Autorrellenar formulario"):
            st.session_state["pendiente_autorrelleno"] = datos
            with estado["lock"]:
                estado["datos"] = None
            st.rerun()
    else:
        st.info("Buscando datos de la factura...")
    if estado.get("error"):
        st.error(f"Error leyendo la cámara: {estado['error']}")
    if estado.get("seg"):
        st.caption(f"Cada lectura demora {estado['seg']:.1f} s")


panel_deteccion()

with st.expander("📁 O subir una foto de la factura"):
    foto = st.file_uploader(
        "Tomar o subir foto de factura PRISA o PRILOGI",
        type=["jpg", "jpeg", "png"]
    )

if foto is not None:
    firma = hashlib.md5(foto.getvalue()).hexdigest()

    # Procesa solo si esta foto todavía no se terminó de procesar
    if st.session_state.get("foto_procesada") != firma:
        try:
            with st.spinner("Leyendo factura... puede tardar unos segundos"):
                img = ImageOps.exif_transpose(Image.open(foto)).convert("RGB")
                img.thumbnail((1800, 1800))  # fotos del celular son enormes
                imagen = np.array(img)
                ocr = cargar_ocr()(imagen)
                lineas = [l.strip() for l in (ocr.txts or []) if l.strip()]
                st.session_state["texto_ocr"] = "\n".join(lineas)
                datos = extraer_datos(lineas)

                if not datos:
                    st.session_state["resultado"] = (
                        "warning",
                        "No pude encontrar los datos del cliente en la factura. "
                        "Revisa el texto detectado o intenta con otra foto."
                    )
                else:
                    nombre_f, dir_f, comuna_f = datos
                    # Deja siempre los datos leídos en el formulario para poder corregirlos
                    st.session_state["nombre_cliente"] = nombre_f
                    st.session_state["direccion_cliente"] = dir_f
                    st.session_state["comuna_cliente"] = comuna_f

                    lat, lon = obtener_coordenadas(dir_f, comuna_f)
                    ya_existe = (
                        supabase.table("Clientes")
                        .select("id")
                        .eq("Cliente", nombre_f)
                        .eq("Direccion", dir_f)
                        .execute()
                        .data
                    )

                    if lat is None:
                        st.session_state["resultado"] = (
                            "warning",
                            f"No encontré la dirección «{dir_f}, {comuna_f}». "
                            "Corrígela abajo (calle y número reales) y guarda."
                        )
                    elif ya_existe:
                        st.session_state["resultado"] = (
                            "info",
                            f"{nombre_f} ya estaba registrado."
                        )
                    else:
                        supabase.table("Clientes").insert({
                            "Cliente": nombre_f,
                            "Direccion": dir_f,
                            "Comuna": comuna_f,
                            "Latitud": lat,
                            "Longitud": lon
                        }).execute()
                        st.session_state["resultado"] = (
                            "success",
                            f"✅ {nombre_f} guardado: {dir_f}, {comuna_f}"
                        )

            # Solo se marca como procesada si terminó bien
            st.session_state["foto_procesada"] = firma

        except Exception as e:
            st.session_state["resultado"] = (
                "error",
                f"Error al procesar la foto: {e}"
            )

    tipo, mensaje = st.session_state.get("resultado", (None, None))
    if tipo:
        getattr(st, tipo)(mensaje)
    else:
        st.info("Foto recibida. Si no aparece nada, toca «Procesar de nuevo».")

    if "texto_ocr" in st.session_state:
        with st.expander("Texto detectado en la factura"):
            st.text(st.session_state["texto_ocr"])

    if st.button("🔄 Procesar de nuevo"):
        st.session_state.pop("foto_procesada", None)
        st.rerun()

# ---------------------------------------------------------------
# Registro manual (por si falla el escaneo)
# ---------------------------------------------------------------
st.subheader("📋 Registrar cliente manualmente")

if "pendiente_autorrelleno" in st.session_state:
    n_p, d_p, c_p = st.session_state.pop("pendiente_autorrelleno")
    st.session_state["nombre_cliente"] = n_p
    st.session_state["direccion_cliente"] = d_p
    st.session_state["comuna_cliente"] = c_p

with st.form("registro_cliente"):
    nombre = st.text_input("Nombre del cliente", key="nombre_cliente")
    direccion = st.text_input("Dirección", key="direccion_cliente")
    comuna = st.text_input("Comuna", key="comuna_cliente")

    guardar = st.form_submit_button("💾 Guardar cliente")

    if guardar:
        if nombre and direccion and comuna:
            latitud, longitud = obtener_coordenadas(direccion, comuna)
            if latitud is None or longitud is None:
                st.error("Dirección no encontrada")
                st.stop()

            supabase.table("Clientes").insert({
                "Cliente": nombre,
                "Direccion": direccion,
                "Comuna": comuna,
                "Latitud": latitud,
                "Longitud": longitud
            }).execute()
            st.success(f"Cliente {nombre} guardado correctamente")
        else:
            st.warning("Completa todos los campos")

# ---------------------------------------------------------------
# Lista de clientes
# ---------------------------------------------------------------
st.subheader("📋 Lista de clientes")

respuesta = supabase.table("Clientes").select(
    "id,Cliente,Direccion,Comuna,Latitud,Longitud"
).order("id").execute()

if respuesta.data:
    st.dataframe(respuesta.data, hide_index=True)
else:
    st.info("Todavía no hay clientes registrados")

# ---------------------------------------------------------------
# Planificar ruta del día
# ---------------------------------------------------------------
st.subheader("🚚 Planificar ruta del día")

if respuesta.data:
    opciones = {
        f"{c['Cliente']} - {c['Comuna']} (ID {c['id']})": c["id"]
        for c in respuesta.data
    }

    seleccionados = st.multiselect(
        "Selecciona los clientes que Gabo visitará:",
        options=list(opciones.keys())
    )

    st.info(f"Clientes seleccionados: {len(seleccionados)}")

    ids_seleccionados = [opciones[n] for n in seleccionados]
    clientes_ruta = [c for c in respuesta.data if c["id"] in ids_seleccionados]

    if clientes_ruta:
        coordenadas_ruta = [(BODEGA_LATITUD, BODEGA_LONGITUD)]

        for c in clientes_ruta:
            if c.get("Latitud") is not None and c.get("Longitud") is not None:
                coordenadas_ruta.append((c["Latitud"], c["Longitud"]))

        if len(coordenadas_ruta) - 1 != len(clientes_ruta):
            st.error(
                "Hay clientes sin coordenadas. "
                "Revisa sus direcciones antes de calcular la ruta."
            )
            st.stop()

        if len(clientes_ruta) > 8:
            st.warning("Por ahora puedes seleccionar hasta 8 clientes por ruta.")
        else:
            try:
                distancias, duraciones = obtener_matriz_rutas(coordenadas_ruta)
                st.success("Distancias por carretera calculadas correctamente")

                mejor_orden, distancia_total = optimizar_ruta(distancias)
                orden_clientes = [clientes_ruta[i - 1] for i in mejor_orden[1:-1]]

                direcciones_maps = (
                    [BODEGA_DIRECCION]
                    + [c["Direccion"] + ", " + c["Comuna"] + ", Chile" for c in orden_clientes]
                    + [BODEGA_DIRECCION]
                )

                st.success(f"Ruta optimizada: {distancia_total / 1000:.2f} km")

                st.subheader("📍 Orden de visitas")
                for posicion, indice in enumerate(mejor_orden):
                    if indice == 0:
                        st.write(f"{posicion + 1}. 🏠 {BODEGA_NOMBRE}")
                    else:
                        st.write(f"{posicion + 1}. 🚚 {clientes_ruta[indice - 1]['Cliente']}")

                st.subheader("🚚 Distancia por tramo")
                for i in range(len(mejor_orden) - 1):
                    km_tramo = distancias[mejor_orden[i]][mejor_orden[i + 1]] / 1000
                    origen = BODEGA_NOMBRE if mejor_orden[i] == 0 else clientes_ruta[mejor_orden[i] - 1]["Cliente"]
                    destino = BODEGA_NOMBRE if mejor_orden[i + 1] == 0 else clientes_ruta[mejor_orden[i + 1] - 1]["Cliente"]
                    st.write(f"🚚 {origen} → {destino}: {km_tramo:.2f} km")

                url_maps = "https://www.google.com/maps/dir/?" + urlencode({
                    "api": 1,
                    "origin": direcciones_maps[0],
                    "destination": direcciones_maps[-1],
                    "waypoints": "|".join(direcciones_maps[1:-1]),
                    "travelmode": "driving"
                })
                st.link_button("🗺️ Abrir ruta en Google Maps", url_maps)

            except (requests.RequestException, ValueError, KeyError, TypeError):
                st.error("No se pudieron consultar las distancias. Intenta nuevamente.")
else:
    st.warning("Primero debes registrar clientes.")