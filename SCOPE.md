# ALCANCE DEL SISTEMA — NORA (Network Operations & Remediation Assistant)

**Versión:** 1.0.0-draft  
**Licencia:** Apache 2.0  
**Repositorio Oficial:** https://github.com/alexandervazquez98/nora.git  
**Fecha:** Septiembre 2026  

---

## 1. Propósito y Visión

**NORA** (*Network Operations & Remediation Assistant*) es una plataforma de software modular, agnóstica y de código abierto diseñada para asistir a operadores de NOC y cuadrillas de campo en la telemetría continua, diagnóstico de causa raíz y remediación asistida de infraestructuras de red críticas (enlaces inalámbricos punto a punto y multipunto, switches de acceso/distribución PoE y ecosistemas de videovigilancia IP).

El sistema opera bajo una filosofía **Agnóstica y Desacoplada**:
* El núcleo del software no contiene direcciones IP, nombres de sitios, credenciales ni topologías fijas.
* Toda la infraestructura objetivo se define de forma dinámica a través de inventarios parametrizados y variables de entorno seguras.
* El control de cambios permanece estrictamente bajo supervisión humana (**Human-in-the-Loop - HITL**).

---

## 2. Gestión de Información Sensible y Privacidad (Zero-Leakage Policy)

Para garantizar la seguridad de la infraestructura y el cumplimiento de normativas de confidencialidad:

1. **Aislamiento de Configuración e Infraestructura:**
   * Cero valores codificados en duro (*zero hardcoded values*). Todas las credenciales SNMP, llaves SSH, tokens y direcciones de red se cargan en tiempo de ejecución vía archivos `.env` o gestores de secretos.
   * Plantillas públicas saneadas (`.env.example`) sin datos reales.
2. **Pipeline de Anonimización y Saneamiento de Telemetría:**
   * Antes de enviar datos a modelos de lenguaje o interfaces externas, la telemetría pasa por un filtro de saneamiento que enmascara identidades internas (IPs privadas, MAC addresses, números de serie y nombres de host) mediante alias sintéticos (ej. `RADIO_NODE_A`, `SWITCH_ACC_01`).
3. **Privacidad de Inferencia Local vs Nube:**
   * Capacidad de operar en entornos aislados (*air-gapped*) o de alta seguridad mediante LLMs locales sin salida a internet.

---

## 3. Capa de Modelos de Lenguaje (Multi-LLM Engine)

NORA incorpora un motor de inferencia desacoplado mediante un patrón de fábrica/adaptador (*Provider Interface*), soportando dos modalidades principales:

### A. Inferencia Local y Privada: LM Studio
* **Protocolo:** API REST compatible con especificación OpenAI (`http://localhost:1234/v1` o endpoint configurable).
* **Propósito:** Diagnóstico en entornos cerrados, sin salida a internet o cuando la política de seguridad prohíbe el envío de telemetría a servicios externos.
* **Características:**
  * Cero costos de API y latencia de red eliminada.
  * Compatibilidad con modelos locales cuantizados (ej. Llama 3, Qwen 2.5, DeepSeek, Mistral).
  * Consumo de recursos ajustable según la capacidad de hardware local (GPU/Metal/CPU).

### B. Inferencia en la Nube de Alta Capacidad: Google Gemini API
* **SDK:** SDK oficial de Google GenAI (`google-genai`).
* **Propósito:** Razonamiento complejo, análisis de correlación causal profunda, soporte multimodal (análisis de capturas de espectro RF y diagramas de perfil de terreno) y ventanas de contexto masivas para logs extensos.
* **Características:**
  * Modelos soportados: Gemini 2.5 Flash (diagnóstico ultrarrápido) y Gemini 2.5 Pro (razonamiento analítico profundo).
  * Autenticación segura vía API Key gestionada en entorno (`GEMINI_API_KEY`).
  * Estricto saneamiento previo de telemetría antes de la llamada al SDK.

### C. Estrategia Híbrida y Fallback
* El sistema permite seleccionar el proveedor activo mediante configuración (`NORA_LLM_PROVIDER=lmstudio` o `NORA_LLM_PROVIDER=gemini`).
* Opción de conmutación por fallo (*fallback* automático): Si el servicio local no responde o requiere un análisis multimodal más complejo, puede derivar la consulta saneada al proveedor cloud (si está autorizado).

---

## 4. Alcance Funcional del Sistema (In-Scope)

### A. Telemetría y Monitoreo Agnóstico (Solo Lectura)
* **Drivers de Radios de Microondas (Familia Cambium PTP/PMP):**
  * Consulta SNMP estándar y propietaria: RSSI, SNR, nivel de modulación adaptativa, capacidad de throughput, errores de trama RF y contadores de interfaz Ethernet.
* **Drivers de Switches de Conmutación y Distribución:**
  * Conexión segura SSH (Netmiko) y SNMP: estado operacional de puertos, errores físicos (CRC, drops, jabbers), negociación y monitoreo de consumo PoE (Watts entregados vs capacidad).
* **Supervisión de Dispositivos Terminales y Video IP:**
  * Métricas de disponibilidad ICMP (latencia, jitter, pérdida consecutiva).
  * Verificación de transporte y salud de streams de video (RTSP / ONVIF).

### B. Motor de Diagnóstico y Razonamiento
* **Correlación de Eventos Multicapa:** Análisis cruzado entre la salud del enlace de transporte (RF), la capa de conmutación (switch/PoE) y la experiencia del servicio terminal.
* **Reconocimiento de Patrones RF:** Detección asistida de atenuación por clima, reflexiones multicamino (*multipath*), desalineación física o saturación de espectro.
* **Generación de Acciones Remediativas Jerarquizadas:** Recomendación de 2 a 3 alternativas técnicas ponderadas por riesgo e impacto en el servicio.

### C. Seguridad Operativa y Human-in-the-Loop (HITL)
* **Cero Modificaciones Autónomas:** NINGÚN comando de escritura, cambio de frecuencia, reinicio de puerto PoE o reseteo de equipo se efectúa de manera desatendida.
* **Flujo de Aprobación Formal (`ChangeRequest`):**
  1. La IA propone la acción con justificación y nivel de riesgo.
  2. Se genera una solicitud estructurada con token único de validación.
  3. El operador humano revisa y aprueba o rechaza explícitamente la acción.
* **Safety Rollback Watchdog:** Todo cambio aplicado a nivel de radiofrecuencia o conmutación crítica activa un temporizador de prueba. Si la conectividad de gestión se interrumpe y no se confirma en el tiempo límite, el cambio se revierte automáticamente.

### D. Interfaces de Comunicación
* **Servidor FastMCP (Model Context Protocol):** Exposición de herramientas y recursos para que agentes LLM interactúen con la telemetría y generen propuestas bajo esquema de tipos estricto.
* **Microservicio REST (FastAPI):** API abierta para integración con dashboards operativos, herramientas de visualización y bots de mensajería para técnicos en campo.

---

## 5. Fuera del Alcance (Non-Goals)

1. **Auto-reparación no supervisada:** NORA nunca actúa como un agente autónomo destructor de red.
2. **Almacenamiento de flujos de video (NVR/VMS):** No procesa almacenamiento ni retención de grabaciones de cámaras.
3. **Control de enrutamiento WAN o BGP global:** No gestiona la topología de ruteo de operadores externos.
4. **Almacenamiento de secretos o topologías en el código base:** Prohibido el commit de credenciales, direcciones IP reales o datos específicos de infraestructura.

---

## 6. Roadmap de Implementación

| Fase | Nombre | Entregables Principales |
| :---: | :--- | :--- |
| **Fase 1** | **Fundación Agnóstica & Multi-LLM** | Configuración base segura (`Pydantic Settings`), pipeline de saneamiento/anonimización de telemetría y conector multi-proveedor (**LM Studio** local + **Google Gemini** oficial). |
| **Fase 2** | **Capa de Drivers y Telemetría** | Drivers agnósticos en Python para SNMP (Cambium PTP) y SSH (Switches PoE). Módulo de monitoreo de disponibilidad de endpoints. |
| **Fase 3** | **Controlador de Seguridad HITL** | Máquina de estados `ChangeRequest`, generación y validación de tokens de aprobación y temporizador de rollback seguro. |
| **Fase 4** | **Interfaces FastMCP & API REST** | Servidor FastMCP para asistentes de IA y microservicio FastAPI para integración con sistemas de monitoreo y bots de campo. |
| **Fase 5** | **Diagnóstico Avanzado & Módulos RF** | Integración con datos de perfiles de enlace, estimación de diversidad espacial y herramientas de análisis de interferencia. |

