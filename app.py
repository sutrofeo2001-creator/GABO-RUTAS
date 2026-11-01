import streamlit as st
import hmac
from supabase import create_client

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

# Conectar con Supabase
supabase = create_client(
    st.secrets["SUPABASE_URL"],
    st.secrets["SUPABASE_SERVICE_ROLE_KEY"]
)

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
            supabase.table("clientes").insert({
                "nombre": nombre,
                "direccion": direccion,
                "comuna": comuna
            }).execute()
            st.success(f"Cliente {nombre} guardado correctamente")
            
        else:
            st.warning("Completa todos los campos")
