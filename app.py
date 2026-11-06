import streamlit as st
import hmac
from itertools import permutations
from supabase import create_client
import requests
from urllib.parse import urlencode
# Pedir contraseña antes de entrar
password = st.text_input(
    "🔒 Contraseña de acceso",
    type="password"
)

if not hmac.compare_digest(
    password,
    st.secrets["APP_PASSWORD"]
):
    if password:
        st.error("Contraseña incorrecta")
    st.stop()
# Buscar coordenadas automáticamente
def obtener_coordenadas(direccion, comuna):
    direccion_completa = f"{direccion}, {comuna}, Chile"

    try:
        respuesta_geo = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": direccion_completa,
                "format": "json",
                "limit": 5,
                "countrycodes": "cl"
            },
            headers={
                "User-Agent": "GaboRutas/1.0 (aplicacion de reparto)"
            },
            timeout=10
        )
        respuesta_geo.raise_for_status()
        resultados = respuesta_geo.json()

        if resultados:
            st.session_state["opciones_direcciones"] 
            = resultados
               return (
                float(resultados[0]["lat"]),
                float(resultados[0]["lon"])
            )
    except (requests.RequestException, ValueError, KeyError):
        pass

    return None, None

# Conectar con Supabase
supabase = create_client(
    st.secrets["SUPABASE_URL"],
    st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
)
# Consultar distancias por carretera
def obtener_matriz_rutas(coordenadas):
    puntos = ";".join(
        f"{lon},{lat}" for lat, lon in coordenadas
    )

    url = (
        "https://router.project-osrm.org/table/v1/driving/"
        + puntos
    )

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
    # Buscar el recorrido mas corto
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
# Bodega principal de GABO RUTAS
BODEGA_NOMBRE = "Bodega Quilicura"
BODEGA_DIRECCION = "Av. Presidente Eduardo Frei Montalva 8301, Quilicura"
BODEGA_LATITUD = -33.361074
BODEGA_LONGITUD = -70.697689
# Pantalla principal
st.title("🚚 GABO RUTAS")
st.write("Bienvenido al planificador de rutas de reparto")

st.subheader("📋 Registrar nuevo cliente")

with st.form("registro_cliente"):
    nombre = st.text_input("Nombre del cliente")
    direccion = st.text_input("Dirección")
    comuna = st.text_input("Comuna")

    guardar = st.form_submit_button("💾 Guardar cliente")

    if guardar:
        if nombre and direccion and comuna:
            latitud, longitud = obtener_coordenadas(direccion, comuna)
            if latitud is None:
                st.error("Dirección no encontrada")
                st.stop()
            if longitud is None:
                st.error("Dirección no encontrada")
                st.stop()
            #cordenadas obtenidas automáticamente 
            
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

# Mostrar clientes guardados
st.subheader("📋 Lista de clientes")

respuesta = supabase.table("Clientes").select(
    "id,Cliente,Direccion,Comuna,Latitud,Longitud"
).order("id").execute()

if respuesta.data:
    st.dataframe(respuesta.data, hide_index=True)
else:
    st.info("Todavía no hay clientes registrados")


# Seleccionar clientes para la ruta del día
st.subheader("🚚 Planificar ruta del día")

if respuesta.data:
    opciones = {
        f"{cliente['Cliente']} - {cliente['Comuna']} (ID {cliente['id']})": cliente['id']
        for cliente in respuesta.data
    }

    seleccionados = st.multiselect(
        "Selecciona los clientes que Gabo visitará:",
        options=list(opciones.keys())
    )

    st.info(f"Clientes seleccionados: {len(seleccionados)}")
    clientes_ruta = [
    cliente
    for cliente in respuesta.data
    if cliente["id"] in [
        opciones[nombre] for nombre in seleccionados
    ]
    ]
    if clientes_ruta:
            coordenadas_ruta = [
                (BODEGA_LATITUD, BODEGA_LONGITUD)
            ]

            for cliente in clientes_ruta:
                if (
            cliente.get("Latitud") is not None
            and cliente.get("Longitud") is not None
        ):
                    coordenadas_ruta.append(
                        (cliente["Latitud"], cliente["Longitud"])
                    )
            if len(coordenadas_ruta) - 1 != len(clientes_ruta):
                st.error("Hay clientes sin coordenadas. Revisa sus direcciones antes de calcular la ruta.")
                st.stop()
            if len(clientes_ruta) > 8:
                st.warning("Por ahora puedes seleccionar hasta 8 clientes por ruta.")
            else:
                try:
                    distancias, duraciones = obtener_matriz_rutas(
                        coordenadas_ruta
                    )
                    st.success("Distancias por carretera calculadas correctamente")
                    mejor_orden, distancia_total = optimizar_ruta(distancias)
                    orden_clientes = [clientes_ruta[i - 1] for i in mejor_orden[1:-1]]
                    direcciones_maps = [BODEGA_DIRECCION] + [c["Direccion"] + ", " + c["Comuna"] + ", Chile" for c in orden_clientes] + [BODEGA_DIRECCION]
                    st.success(f"Ruta optimizada: {distancia_total / 1000:.2f} km")
                    st.subheader("📍 Orden de visitas")
                    st.subheader("🚚 Distancia por tramo")
                    for i in range(len(mejor_orden) - 1):
                        km_tramo = distancias[mejor_orden[i]][mejor_orden[i + 1]] / 1000
                        origen = "Bodega Quilicura" if mejor_orden[i] == 0 else clientes_ruta[mejor_orden[i] - 1]["Cliente"]
                        destino = "Bodega Quilicura" if mejor_orden[i + 1] == 0 else clientes_ruta[mejor_orden[i + 1] - 1]["Cliente"]
                        st.write(f"🚚 {origen} → {destino}: {km_tramo:.2f} km")
                    for posicion, indice in enumerate(mejor_orden):
                      if indice == 0:
                            st.write(f"{posicion + 1}. 🏠 Bodega Quilicura")
                      else:
                            st.write(f"{posicion + 1}. 🚚 {clientes_ruta[indice - 1]['Cliente']}")
                    url_maps = "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "origin": direcciones_maps[0], "destination": direcciones_maps[-1], "waypoints": "|".join(direcciones_maps[1:-1]), "travelmode": "driving"})
                    st.link_button("🗺️ Abrir ruta en Google Maps", url_maps)
                except (requests.RequestException, ValueError, KeyError, TypeError):
                    st.error("No se pudieron consultar las distancias. Intenta nuevamente.")
else:
    st.warning("Primero debes registrar clientes.")
