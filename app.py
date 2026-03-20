import streamlit as st
import pandas as pd
 
st.set_page_config(page_title="Comparador Chile", page_icon="📡")
 
st.title("📡 Mi Comparador Telecom")
 
# Sidebar para datos sensibles
with st.sidebar:
    st.header("Configuración")
    rut = st.text_input("RUT (para factibilidad)")
    dir_completa = st.text_input("Dirección y Comuna")
 
# Cuerpo principal: Selección de servicios
st.subheader("¿Qué servicios necesitas?")
col1, col2 = st.columns(2)
with col1:
    quiere_internet = st.checkbox("🌐 Internet Fibra")
    quiere_movil = st.checkbox("📱 Telefonía Móvil")
with col2:
    quiere_tv = st.checkbox("📺 TV Cable")
    quiere_fija = st.checkbox("☎️ Telefonía Fija")
 
if st.button("Buscar la mejor oferta 🚀", use_container_width=True):
    if not rut or not dir_completa:
        st.error("Por favor completa tu RUT y Dirección en la barra lateral.")
    else:
        with st.spinner("Consultando bases de datos..."):
            # Aquí simulamos la lógica que el bot procesará
            # En una fase avanzada, aquí llamaríamos a Playwright
            data = [
                {"Compañía": "Mundo", "Servicio": "Internet", "Precio": 15990},
                {"Compañía": "Entel", "Servicio": "Móvil", "Precio": 9990},
                {"Compañía": "VTR", "Servicio": "Internet + TV", "Precio": 21990}
            ]
            st.success("¡Ofertas encontradas para tu zona!")
            st.table(pd.DataFrame(data))
