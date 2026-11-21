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
from urllib.parse import urlencode, unquote_plus
from supabase import create_client
from rapidocr import RapidOCR
from streamlit_webrtc import webrtc_streamer, WebRtcMode
from PIL import Image, ImageOps

# Conexiones y configuración
supabase = create_client(
    st.secrets["SUPABASE_URL"],
    st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
)


# Funciones
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

    for i, variante in enumerate(variantes):
        if i > 0:
            time.sleep(1)  # Nominatim pide máximo 1 consulta por segundo
        lat, lon = _buscar_nominatim(variante)
        if lat is not None:
            return lat, lon
    return None, None


def _parsear_coordenadas(texto):
    patrones = (
        r"@(-?\d+\.\d+),(-?\d+\.\d+)",
        r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
        r"(-?\d{1,3}\.\d+)\s*[,;\s]\s*(-?\d{1,3}\.\d+)",
    )
    for patron in patrones:
        m = re.search(patron, texto)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None, None


def leer_enlace(texto):
    # Acepta coordenadas ("-33.36, -70.69") o un enlace de Google Maps.
    # Devuelve (lat, lon, nombre_del_lugar). El nombre puede venir vacío.
    texto = texto.strip()
    nombre = ""
    if texto.startswith("http"):
        try:
            r = requests.get(
                texto, allow_redirects=True, timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            texto = r.url
        except requests.RequestException:
            pass
        m = re.search(r"/maps/place/([^/@?]+)", texto)
        if m:
            nombre = unquote_plus(m.group(1)).strip()
            if re.match(r"^-?\d", nombre):  # si es solo coordenadas, no sirve de nombre
                nombre = ""
    lat, lon = _parsear_coordenadas(texto)
    return lat, lon, nombre


def leer_coordenadas(texto):
    lat, lon, _ = leer_enlace(texto)
    return lat, lon


def direccion_desde_coordenadas(lat, lon):
    # Busca la dirección y comuna que corresponden a unas coordenadas
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat, "lon": lon, "format": "jsonv2",
                "addressdetails": 1, "zoom": 18, "accept-language": "es"
            },
            headers={"User-Agent": "GaboRutas/1.0 (aplicacion de reparto)"},
            timeout=10
        )
        r.raise_for_status()
        datos = r.json()
        a = datos.get("address", {})
        calle = a.get("road") or a.get("pedestrian") or a.get("path") or ""
        if calle:
            direccion = f"{calle} {a.get('house_number', '')}".strip()
        else:
            direccion = datos.get("name") or "Ubicación por coordenadas"
        comuna = (
            a.get("city") or a.get("town") or a.get("village")
            or a.get("municipality") or a.get("suburb") or a.get("county") or ""
        )
        return direccion, comuna
    except (requests.RequestException, ValueError):
        return None, None


def ubicar(nombre, direccion, comuna):
    # Devuelve (lat, lon, nivel). nivel: "direccion", "empresa" o "comuna"
    lat, lon = obtener_coordenadas(direccion, comuna)
    if lat is not None:
        return lat, lon, "direccion"
    time.sleep(1)
    lat, lon = _buscar_nominatim(f"{nombre}, {comuna}, Chile")
    if lat is not None:
        return lat, lon, "empresa"
    time.sleep(1)
    lat, lon = _buscar_nominatim(f"{comuna}, Chile")
    if lat is not None:
        return lat, lon, "comuna"
    return None, None, None


AVISO_NIVEL = {
    "exacta": "",
    "direccion": "",
    "empresa": " ⚠️ Ubicación según el nombre de la empresa: revísala.",
    "comuna": " ⚠️ Ubicación aproximada (centro de la comuna).",
}


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
# Empresas y acceso
def hash_clave(codigo, clave):
    sal = ("gaborutas:" + codigo.strip().lower()).encode()
    return hashlib.pbkdf2_hmac("sha256", clave.encode(), sal, 200_000).hex()


def datos_bodega(direccion, ubicacion):
    # Coordenadas de la bodega: desde coordenadas/enlace o desde la dirección
    if ubicacion.strip():
        lat, lon, _ = leer_enlace(ubicacion)
        return lat, lon
    return _buscar_nominatim(f"{direccion}, Chile")


if "empresa" not in st.session_state:
    st.session_state["empresa"] = None
if "es_admin" not in st.session_state:
    st.session_state["es_admin"] = False

if st.session_state["empresa"] is None and not st.session_state["es_admin"]:
    st.title("🚚 GABO RUTAS")
    codigo_in = st.text_input("Código de empresa")
    clave_in = st.text_input("🔒 Contraseña", type="password")
    if st.button("Entrar"):
        codigo_n = codigo_in.strip().lower()
        if codigo_n == "admin":
            if hmac.compare_digest(clave_in, st.secrets["APP_PASSWORD"]):
                st.session_state["es_admin"] = True
                st.rerun()
            else:
                st.error("Código o contraseña incorrectos")
        else:
            filas = (
                supabase.table("Empresas").select("*")
                .eq("codigo", codigo_n).execute().data
            )
            if filas and hmac.compare_digest(
                filas[0]["clave_hash"], hash_clave(codigo_n, clave_in)
            ):
                st.session_state["empresa"] = filas[0]
                st.rerun()
            else:
                st.error("Código o contraseña incorrectos")
    st.stop()

# Panel de administrador (código "admin" + la clave APP_PASSWORD)
if st.session_state["es_admin"]:
    st.title("🛠️ Administrador")
    if st.button("🚪 Cerrar sesión"):
        st.session_state.clear()
        st.rerun()

    st.subheader("➕ Crear empresa")
    with st.form("crear_empresa"):
        c_codigo = st.text_input("Código de acceso (sin espacios, ej: prisa)")
        c_nombre = st.text_input("Nombre de la empresa")
        c_clave = st.text_input("Contraseña de la empresa", type="password")
        c_bnombre = st.text_input("Nombre de la bodega", value="Bodega principal")
        c_bdir = st.text_input("Dirección de la bodega (con comuna)")
        c_bubic = st.text_input(
            "Ubicación exacta de la bodega (opcional)",
            help="Coordenadas o enlace de Google Maps."
        )
        crear = st.form_submit_button("Crear empresa")

        if crear:
            codigo_n = c_codigo.strip().lower()
            if not (codigo_n and c_nombre.strip() and c_clave and c_bdir.strip()):
                st.warning("Completa código, nombre, contraseña y dirección de la bodega.")
            elif codigo_n == "admin" or " " in codigo_n:
                st.error("Ese código no es válido (sin espacios y distinto de «admin»).")
            else:
                lat_b, lon_b = datos_bodega(c_bdir, c_bubic)
                existe = (
                    supabase.table("Empresas").select("id")
                    .eq("codigo", codigo_n).execute().data
                )
                if existe:
                    st.error("Ya existe una empresa con ese código.")
                elif lat_b is None:
                    st.error("No encontré la bodega. Pega la ubicación exacta.")
                else:
                    supabase.table("Empresas").insert({
                        "codigo": codigo_n,
                        "nombre": c_nombre.strip(),
                        "clave_hash": hash_clave(codigo_n, c_clave),
                        "bodega_nombre": c_bnombre.strip() or "Bodega principal",
                        "bodega_direccion": c_bdir.strip(),
                        "bodega_latitud": lat_b,
                        "bodega_longitud": lon_b,
                    }).execute()
                    st.success(f"Empresa {c_nombre.strip()} creada. Código de acceso: {codigo_n}")

    empresas = (
        supabase.table("Empresas")
        .select("id,codigo,nombre,bodega_nombre,bodega_direccion")
        .order("id").execute().data
    )
    st.subheader("🏢 Empresas")
    if empresas:
        st.dataframe(empresas, hide_index=True)
        st.subheader("🔑 Cambiar contraseña de una empresa")
        with st.form("cambiar_clave"):
            e_id = st.selectbox(
                "Empresa",
                options=[e["id"] for e in empresas],
                format_func=lambda i: next(e["nombre"] for e in empresas if e["id"] == i)
            )
            nueva_clave = st.text_input("Nueva contraseña", type="password")
            if st.form_submit_button("Cambiar contraseña"):
                if nueva_clave:
                    cod_e = next(e["codigo"] for e in empresas if e["id"] == e_id)
                    supabase.table("Empresas").update(
                        {"clave_hash": hash_clave(cod_e, nueva_clave)}
                    ).eq("id", e_id).execute()
                    st.success("Contraseña cambiada")
                else:
                    st.warning("Escribe la nueva contraseña.")
    else:
        st.info("Todavía no hay empresas creadas.")
    st.stop()

# Empresa que inició sesión: todo lo que sigue usa SUS datos
EMPRESA = st.session_state["empresa"]
EMPRESA_ID = EMPRESA["id"]
BODEGA_NOMBRE = EMPRESA["bodega_nombre"]
BODEGA_DIRECCION = EMPRESA["bodega_direccion"]
BODEGA_LATITUD = EMPRESA["bodega_latitud"]
BODEGA_LONGITUD = EMPRESA["bodega_longitud"]

# Pantalla principal
st.title("🚚 GABO RUTAS")
st.write(f"🏢 **{EMPRESA['nombre']}**")
if st.button("🚪 Cerrar sesión"):
    st.session_state.clear()
    st.rerun()

with st.expander("⚙️ Bodega de la empresa"):
    st.write(f"**{BODEGA_NOMBRE}**: {BODEGA_DIRECCION}")
    with st.form("editar_bodega"):
        b_nombre = st.text_input("Nombre de la bodega", value=BODEGA_NOMBRE)
        b_dir = st.text_input("Dirección de la bodega (con comuna)", value=BODEGA_DIRECCION)
        b_ubic = st.text_input(
            "Ubicación exacta (opcional)",
            help="Coordenadas o enlace de Google Maps."
        )
        if st.form_submit_button("Guardar bodega"):
            lat_n, lon_n = datos_bodega(b_dir, b_ubic)
            if not b_dir.strip():
                st.warning("Escribe la dirección de la bodega.")
            elif lat_n is None:
                st.error("No encontré esa dirección. Pega la ubicación exacta.")
            else:
                nuevos = {
                    "bodega_nombre": b_nombre.strip() or "Bodega",
                    "bodega_direccion": b_dir.strip(),
                    "bodega_latitud": lat_n,
                    "bodega_longitud": lon_n,
                }
                supabase.table("Empresas").update(nuevos).eq("id", EMPRESA_ID).execute()
                st.session_state["empresa"] = {**EMPRESA, **nuevos}
                st.success("Bodega actualizada. Se aplica en la próxima ruta que calcules.")

# Escanear factura (automático: foto -> OCR -> guardar)
st.subheader("📸 Escanear factura en vivo")
st.caption("Enciende la cámara y apunta a la zona «SEÑOR(ES)» de la factura.")

# Estado de la cámara: uno por sesión (cada empresa ve solo lo suyo)
if "estado_escaner" not in st.session_state:
    st.session_state["estado_escaner"] = {
        "lock": threading.Lock(), "ocupado": False, "ultimo": 0.0, "datos": None
    }
estado = st.session_state["estado_escaner"]
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

                    lat, lon, nivel = ubicar(nombre_f, dir_f, comuna_f)
                    ya_existe = (
                        supabase.table("Clientes")
                        .select("id")
                        .eq("empresa_id", EMPRESA_ID)
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
                            "empresa_id": EMPRESA_ID,
                            "Cliente": nombre_f,
                            "Direccion": dir_f,
                            "Comuna": comuna_f,
                            "Latitud": lat,
                            "Longitud": lon
                        }).execute()
                        st.session_state["resultado"] = (
                            "success" if nivel == "direccion" else "warning",
                            f"✅ {nombre_f} guardado: {dir_f}, {comuna_f}."
                            + AVISO_NIVEL[nivel]
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
# Registro manual (por si falla el escaneo)

st.subheader("⚡ Guardar con solo un enlace de Google Maps")
st.caption(
    "Pega el enlace de la ubicación (o las coordenadas). "
    "La app busca la dirección y guarda el cliente al tiro."
)
enlace_rapido = st.text_input("Enlace de Google Maps o coordenadas", key="enlace_rapido")
nombre_rapido = st.text_input(
    "Nombre del cliente (solo si el enlace no lo trae)", key="nombre_rapido"
)
if st.button("⚡ Buscar y guardar"):
    with st.spinner("Buscando ubicación..."):
        lat_r, lon_r, lugar_r = leer_enlace(enlace_rapido)
        if lat_r is None:
            st.error(
                "No pude sacar la ubicación de ese enlace. "
                "Prueba pegando las coordenadas (ej: -31.7745, -70.9697)."
            )
        else:
            nombre_r = nombre_rapido.strip() or lugar_r
            if not nombre_r:
                st.warning(
                    "El enlace no trae el nombre del lugar. Escribe el nombre "
                    "del cliente arriba y toca el botón otra vez."
                )
            else:
                dir_r, com_r = direccion_desde_coordenadas(lat_r, lon_r)
                dir_r = dir_r or "Ubicación por coordenadas"
                com_r = com_r or "Sin comuna"
                ya_r = (
                    supabase.table("Clientes")
                    .select("id")
                    .eq("empresa_id", EMPRESA_ID)
                    .eq("Cliente", nombre_r)
                    .eq("Direccion", dir_r)
                    .execute()
                    .data
                )
                if ya_r:
                    st.info(f"{nombre_r} ya estaba registrado.")
                else:
                    supabase.table("Clientes").insert({
                        "empresa_id": EMPRESA_ID,
                        "Cliente": nombre_r,
                        "Direccion": dir_r,
                        "Comuna": com_r,
                        "Latitud": lat_r,
                        "Longitud": lon_r
                    }).execute()
                    st.success(f"✅ {nombre_r} guardado: {dir_r}, {com_r}")

st.subheader("📍 Agregar por coordenadas")
st.caption(
    "Pídele las coordenadas al cliente (o saca el punto en Google Maps: "
    "mantén apretado el lugar y copia los números de arriba)."
)
coords_txt = st.text_input(
    "Coordenadas o enlace de Google Maps",
    placeholder="-31.7745, -70.9697",
    key="coords_busqueda"
)
if st.button("🔎 Buscar dirección"):
    lat_c, lon_c = leer_coordenadas(coords_txt)
    if lat_c is None:
        st.error("No entendí las coordenadas. Ejemplo: -31.7745, -70.9697")
    else:
        with st.spinner("Buscando dirección..."):
            dir_c, com_c = direccion_desde_coordenadas(lat_c, lon_c)
        st.session_state["pendiente_coords"] = {
            "direccion": dir_c or "",
            "comuna": com_c or "",
            "ubicacion": f"{lat_c}, {lon_c}",
        }
        st.rerun()

if "pendiente_coords" in st.session_state:
    p = st.session_state.pop("pendiente_coords")
    if p["direccion"]:
        st.session_state["direccion_cliente"] = p["direccion"]
    if p["comuna"]:
        st.session_state["comuna_cliente"] = p["comuna"]
    st.session_state["ubicacion_cliente"] = p["ubicacion"]
    st.session_state["aviso_coords"] = (
        "Dirección encontrada. Escribe el nombre del cliente, revisa los datos y guarda."
        if p["direccion"] else
        "No encontré dirección para esas coordenadas. Escríbela abajo y guarda; la ubicación ya quedó."
    )

if "aviso_coords" in st.session_state:
    st.success(st.session_state.pop("aviso_coords"))

if "pendiente_autorrelleno" in st.session_state:
    n_p, d_p, c_p = st.session_state.pop("pendiente_autorrelleno")
    st.session_state["nombre_cliente"] = n_p
    st.session_state["direccion_cliente"] = d_p
    st.session_state["comuna_cliente"] = c_p

st.subheader("📋 Datos del cliente (revisar y guardar)")
with st.form("registro_cliente"):
    nombre = st.text_input("Nombre del cliente", key="nombre_cliente")
    direccion = st.text_input("Dirección", key="direccion_cliente")
    comuna = st.text_input("Comuna", key="comuna_cliente")

    ubicacion = st.text_input(
        "📍 Ubicación exacta (opcional)",
        help="En Google Maps deja apretado el punto de la faena, copia las "
             "coordenadas (o el enlace de compartir) y pégalo aquí.",
        key="ubicacion_cliente"
    )

    guardar = st.form_submit_button("💾 Guardar cliente")

    if guardar:
        if nombre and direccion and comuna:
            if ubicacion.strip():
                latitud, longitud = leer_coordenadas(ubicacion)
                nivel = "exacta"
                if latitud is None:
                    st.error(
                        "No pude entender esa ubicación. Pega las coordenadas "
                        "(ej: -33.36, -70.69) o un enlace de Google Maps."
                    )
                    st.stop()
            else:
                latitud, longitud, nivel = ubicar(nombre, direccion, comuna)
                if latitud is None:
                    st.error(
                        "No encontré ninguna ubicación. Pega la ubicación "
                        "exacta desde Google Maps."
                    )
                    st.stop()

            supabase.table("Clientes").insert({
                "empresa_id": EMPRESA_ID,
                "Cliente": nombre,
                "Direccion": direccion,
                "Comuna": comuna,
                "Latitud": latitud,
                "Longitud": longitud
            }).execute()
            if nivel in ("exacta", "direccion"):
                st.success(f"Cliente {nombre} guardado correctamente")
            else:
                st.warning(f"Cliente {nombre} guardado." + AVISO_NIVEL[nivel])
        else:
            st.warning("Completa todos los campos")



              