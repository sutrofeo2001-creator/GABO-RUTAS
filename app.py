import streamlit as st

st.title("🚚 GABO RUTAS")
st.write("Bienvenido al planificador de rutas de reparto")
import streamlit as st

st.subheader("📋 Registrar nuevo cliente")

with st.form("registro_cliente"):
    nombre = st.text_input("Nombre del cliente")
    direccion = st.text_input("Dirección")
    comuna = st.text_input("Comuna")

    guardar = st.form_submit_button("💾 Guardar cliente")

    if guardar:
        if nombre and direccion and comuna:
            st.success(f"Cliente {nombre} ingresado correctamente")
        else:
            st.warning("Completa todos los campos")
