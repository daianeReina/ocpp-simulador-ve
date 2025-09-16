# evbox_g3_gui_mock.py
# 
# Simulador EVBox G3 (OCPP 1.6) **com GUI (PyQt5)** e comportamento próximo do real:
# - Conecta na porta 9001 (ou outra que indicar), com subprotocolo `ocpp1.6`.
# - **Não** chama Authorize prévio; o idTag vai **só** no StartTransaction (como EVBox real).
# - Envia pares de `StatusNotification` + `DataTransfer(evbStatusNotification)` em **formato compacto**.
# - Heartbeat default 900s (ajusta se o CSMS retornar outro intervalo no Boot).
# - GUI para conectar, iniciar/parar sessão e visualizar potência/energia e logs.
#
# Requisitos:
#   pip install websockets ocpp PyQt5 qasync
#
# Execução:
#   python evbox_g3_gui_mock.py
#   (depois, na GUI: definir URL, ID do carregador e idTag; clicar Conectar e Iniciar Carregamento)
#

import asyncio
import json
import logging
import sys
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from datetime import datetime, timezone

from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    RegistrationStatus,
    ChargePointStatus,
    ChargePointErrorCode,
    DataTransferStatus,
    ConfigurationStatus,
)
from ocpp.routing import on

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QGroupBox, QLineEdit
)
from PyQt5.QtCore import QTimer
import qasync
from ocpp.v16.enums import ConfigurationStatus
from ocpp.v16 import call_result

# -----------------------------
# Tabela de configurações do mock
# -----------------------------
CONFIG = {
    # OCPP padrão
    "AuthorizeRequired":         {"value": "true",  "readonly": False},
    "HeartbeatInterval":         {"value": "900",   "readonly": False},
    "MeterValueSampleInterval":  {"value": "60",    "readonly": False},

    # Chaves EVBox usuais (mantemos em sincronia com a OCPP acima quando fizer sentido)
    # evb_SkipAuthorize = 1 => NÃO requer autorização | 0 => requer (inverso de AuthorizeRequired)
    "evb_SkipAuthorize":         {"value": "0",     "readonly": False},
}

def _as_bool(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")

# ------------------------- Logging ---------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
)
logger = logging.getLogger("EVB_G3_GUI")

# ------------------------- Helpers ---------------------------------

def utc_iso_z() -> str:
    """Timestamp ISO 8601 em UTC com 'Z', como EVBox costuma enviar."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def build_evb_status_compact(
    connector_id: int,
    status: str,
    error_code: str = "NoError",
    error_desc: str = "No error.",
    led_color: str = "Green",
    plugged: int = 0,
    energy_register_wh: int = 0,
    tx_id: int = 0,
) -> str:
    """Constrói uma string compacta semelhante ao evbStatusNotification.

    O formato exato pode variar; aqui usamos campos plausíveis e estáveis
    suficientes para ilustrar o funcionamento do CSMS.
    """
    group1 = "{32,4784,41}"  # valores internos fictícios 
    group2 = f"{{{energy_register_wh},0}}"  # EnergyActive.Import.Register + delta
    group3 = "{C,11912,5908,11944}"
    group4 = "{245,0,0,0,0,0,1000,0,0}"
    some_num1 = "16"
    some_num2 = "6680"
    some_num3 = "445"
    fw_interval_s = 5000

    vehicle_present = 1 if status in ("Charging", "SuspendedEVSE") else 0
    plugged_flag = int(plugged)

    parts = [
        str(connector_id),
        status,
        error_code,
        error_desc,
        str(vehicle_present),
        led_color,
        str(plugged_flag),
        group1,
        group2,
        group3,
        some_num1,
        some_num2,
        some_num3,
        utc_iso_z(),
        str(tx_id or 0),
        "96",
        group4,
        "49", "0", "0", "0", "0", "0", "250", str(fw_interval_s),
    ]
    return ",".join(parts)

# ------------------------- Charge Point ----------------------------

class EVBoxG3Simulator(BaseChargePoint):
    """Simulador de Charge Point (EVBox G3-like) com GUI."""

    def __init__(self, id: str, connection, gui: 'EVChargerGUI'):
        super().__init__(id, connection)
        self.cp_id = id
        self.gui = gui
        self.connector_id = 1
        self.heartbeat_interval = 900  # típico EVBox
        self.tx_id: Optional[int] = None
        self.energy_wh_counter = 11_905_680  # valor inicial plausível (similar aos logs de exemplo)
        self.charging = False
        self.connected = True

    async def start(self):
        """Inicia o listener OCPP, envia Boot e agenda heartbeat."""
        asyncio.create_task(super().start())
        try:
            self.gui.log_message("🔌 Enviando BootNotification…")
            resp = await self.call(call.BootNotification(
                charge_point_model="G3-M5320E",
                charge_point_vendor="EV-BOX",
                charge_point_serial_number=self.cp_id.replace('_', '-'),
                firmware_version="G3P0134B0125",
            ))
            if resp.status != RegistrationStatus.accepted:
                self.gui.log_message(f"❌ Boot rejeitado: {resp.status}")
                self.connected = False
                return
            try:
                self.heartbeat_interval = int(getattr(resp, 'interval', self.heartbeat_interval) or 900)
            except Exception:
                self.heartbeat_interval = 900
            self.gui.log_message(f"✅ Boot aceito. HeartbeatInterval={self.heartbeat_interval}s")
        except Exception as e:
            self.gui.log_message(f"❌ Erro no BootNotification: {e}")
            self.connected = False
            return

        asyncio.create_task(self._heartbeat_task())

    async def _heartbeat_task(self):
        while self.connected:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                await self.call(call.Heartbeat())
                self.gui.log_message("💓 Heartbeat enviado")
            except ConnectionClosed:
                self.gui.log_message("❌ Conexão fechada durante Heartbeat")
                self.connected = False
                break
            except Exception as e:
                self.gui.log_message(f"❌ Erro no Heartbeat: {e}")
                self.connected = False
                break

    # ---------------------- Fluxo de sessão -----------------------
    async def start_session_flow(self, id_tag: str):
        """
        Inicia um ciclo de sessão simulando o EVBox G3.

        Importante: só continua para Charging se o CSMS aceitar o StartTransaction.
        Caso o CSMS devolva transaction_id <= 0 (ou erro), a sessão é abortada
        e o mock volta para Available.
        """
        if self.charging:
            self.gui.log_message("⚠️ Sessão já em andamento.")
            return

        # 1) Preparing + DataTransfer
        await self.send_status(ChargePointStatus.preparing)
        await self.send_evb_datatransfer("Preparing", led_color="Off", plugged=0)
        await asyncio.sleep(2)

        # 2) StartTransaction (sem Authorize prévio — comportamento EVBox)
        await self.send_start_transaction(id_tag)

        # Verifica se o CSMS aceitou (transaction_id > 0)
        tx_ok = isinstance(self.tx_id, int) and self.tx_id > 0
        if not tx_ok:
            self.gui.log_message("⛔ StartTransaction rejeitado pelo CSMS (idTag inválida ou não autorizada).")
            # Volta para Available e encerra o fluxo
            await self.send_status(ChargePointStatus.available)
            await self.send_evb_datatransfer("Available", led_color="Green", plugged=0)
            self.charging = False
            self.gui.update_charging_status("Disponível")
            return

        # 3) SuspendedEVSE (curto), depois Charging
        await self.send_status(ChargePointStatus.suspended_evse, info="B;425")
        await self.send_evb_datatransfer("SuspendedEVSE", led_color="Yellow", plugged=1)
        await asyncio.sleep(1)

        await self.send_status(ChargePointStatus.charging, info="C;424")
        await self.send_evb_datatransfer("Charging", led_color="Blue", plugged=1)

        self.charging = True
        self.gui.update_charging_status("Carregando…")

        # 4) MeterValues periódicos + DataTransfer
        for i in range(3):
            if not self.charging or not self.connected:
                break
            await asyncio.sleep(10)

            # Incrementa energia simulada (exemplo)
            inc = 1_280  # ~1.28 kWh por amostra
            await self.send_meter_values(increment_wh=inc)

            # Atualiza GUI com potência estimada (visual)
            est_power_w = int(inc * 3600 / 10)  # 10s no sleep acima
            self.gui.update_energy_values(est_power_w, self.energy_wh_counter)

            # DataTransfer de estado
            await self.send_evb_datatransfer("Charging", led_color="Blue", plugged=1)

        # 5) Finalização
        if self.charging:
            await self.send_stop_transaction(reason="Local")
            await self.send_status(ChargePointStatus.finishing)
            await self.send_evb_datatransfer("Finishing", led_color="Green", plugged=0)

            await self.send_status(ChargePointStatus.available)
            await self.send_evb_datatransfer("Available", led_color="Green", plugged=0)
            self.charging = False
            self.gui.update_charging_status("Disponível")


    async def stop_session(self, reason: str = "Local"):
        if not self.charging:
            self.gui.log_message("ℹ️ Não há sessão ativa.")
            return
        self.charging = False
        await self.send_stop_transaction(reason=reason)
        await self.send_status(ChargePointStatus.available)
        await self.send_evb_datatransfer("Available", led_color="Green", plugged=0)
        self.gui.update_charging_status("Disponível")

    # ---------------------- Envio de mensagens --------------------
    async def send_status(self, status: ChargePointStatus, info: Optional[str] = None):
        try:
            req = call.StatusNotification(
                connector_id=self.connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=status,
                timestamp=utc_iso_z(),
                info=info,
            )
            await self.call(req)
            self.gui.log_message(f"📡 StatusNotification: {status.name}{' | '+info if info else ''}")
        except Exception as e:
            self.gui.log_message(f"❌ Falha ao enviar StatusNotification: {e}")

    async def send_evb_datatransfer(self, status_str: str, led_color: str = "Green", plugged: int = 0):
        try:
            payload = build_evb_status_compact(
                connector_id=self.connector_id,
                status=status_str,
                error_code="NoError",
                error_desc="No error.",
                led_color=led_color,
                plugged=plugged,
                energy_register_wh=self.energy_wh_counter,
                tx_id=self.tx_id or 0,
            )
            req = call.DataTransfer(
                vendor_id="EV-BOX",
                message_id="evbStatusNotification",
                data=payload,
            )
            resp = await self.call(req)
            self.gui.log_message(f"📦 DataTransfer evbStatusNotification ({status_str}) → {getattr(resp, 'status', '???')}")
        except Exception as e:
            self.gui.log_message(f"❌ Falha ao enviar DataTransfer: {e}")

    async def send_start_transaction(self, id_tag: str) -> bool:
        try:
            req = call.StartTransaction(
                connector_id=self.connector_id,
                id_tag=id_tag,
                timestamp=utc_iso_z(),
                meter_start=self.energy_wh_counter,
            )
            resp = await self.call(req)
    
            # Lê status dentro de id_tag_info (pode vir dict ou objeto)
            info = getattr(resp, 'id_tag_info', None)
            if isinstance(info, dict):
                status_val = info.get('status')
            else:
                status_val = getattr(info, 'status', None)
                status_val = getattr(status_val, 'value', status_val)  # enum -> string
    
            tx_id = getattr(resp, 'transaction_id', 0)
            accepted = (str(status_val).lower() == 'accepted' and isinstance(tx_id, int) and tx_id > 0)
    
            if not accepted:
                self.gui.log_message(f"⛔ StartTransaction rejeitado. status={status_val} tx={tx_id}")
                self.tx_id = None
                return False
    
            self.tx_id = tx_id
            self.gui.log_message(f"⚡ StartTransaction ACEITO. tx_id={self.tx_id} | meter_start={self.energy_wh_counter} Wh")
            return True
    
        except Exception as e:
            self.gui.log_message(f"❌ Erro no StartTransaction: {e}")
            return False

    async def send_meter_values(self, increment_wh: int = 1000):
        try:
            self.energy_wh_counter += max(1, int(increment_wh))
            req = call.MeterValues(
                connector_id=self.connector_id,
                transaction_id=self.tx_id,
                meter_value=[{
                    "timestamp": utc_iso_z(),
                    "sampledValue": [{
                        "value": str(self.energy_wh_counter),
                        "measurand": "Energy.Active.Import.Register",
                        "unit": "Wh",
                    }]
                }]
            )
            await self.call(req)
            self.gui.log_message(f"🔢 MeterValues: {self.energy_wh_counter} Wh (tx={self.tx_id})")
        except Exception as e:
            self.gui.log_message(f"❌ Falha ao enviar MeterValues: {e}")

    async def send_stop_transaction(self, reason: str = "Local"):
        try:
            req = call.StopTransaction(
                transaction_id=self.tx_id,
                id_tag=self.gui.id_tag,
                timestamp=utc_iso_z(),
                meter_stop=self.energy_wh_counter,
                reason=reason,
            )
            await self.call(req)
            self.gui.log_message(f"🔚 StopTransaction enviado. meter_stop={self.energy_wh_counter} Wh")
            self.tx_id = None
        except Exception as e:
            self.gui.log_message(f"❌ Falha no StopTransaction: {e}")

    # ---------------------- Handlers do CSMS ----------------------
    @on('RemoteStartTransaction')
    async def on_remote_start_transaction(self, connector_id, id_tag, **kwargs):
        self.gui.log_message(f"📥 RemoteStartTransaction: connector={connector_id}, idTag={id_tag}")
        asyncio.create_task(self.start_session_flow(id_tag))
        return call_result.RemoteStartTransaction(status="Accepted")

    @on('RemoteStopTransaction')
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        self.gui.log_message(f"📥 RemoteStopTransaction: tx_id={transaction_id}")
        if self.tx_id == transaction_id:
            await self.stop_session(reason="Remote")
            return call_result.RemoteStopTransaction(status="Accepted")
        return call_result.RemoteStopTransaction(status="Rejected")

    @on('Heartbeat')
    async def on_heartbeat(self, **kwargs):
        self.gui.log_message("💓 Heartbeat recebido do CSMS")
        return call_result.Heartbeat(current_time=utc_iso_z())

    @on('DataTransfer')
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        self.gui.log_message(f"↩️ DataTransfer do CSMS: vendor={vendor_id}, id={message_id}, data={data}")
        return call_result.DataTransfer(status=DataTransferStatus.accepted)
    
    # --- Config local do mock (como CP) ---
    def _ensure_config(self):
        # inicia um dicionário de config se ainda não existir
        if not hasattr(self, "_config"):
            self._config = {
                # OCPP “padrão”
                "AuthorizeRequired": "true",      # EVBox G3 costuma não exigir authorize, ajuste se quiser
                "HeartbeatInterval": "900",
                "MeterValuesSampleInterval": "60",

                # chave “EVBox way” equivalente: evb_SkipAuthorize (1 => não requer Authorize)
                "evb_SkipAuthorize": "0",
            }
        # espelha AuthorizeRequired num atributo de runtime, se quiser usar em outros trechos
        if not hasattr(self, "authorize_required"):
            self.authorize_required = self._config["AuthorizeRequired"].lower() == "true"
    
    def _as_bool(v)->bool:
        return str(v).strip().lower() in ("1","true","yes","on")

    @on('GetConfiguration')
    async def on_get_configuration(self, key=None, **kwargs):
        self._ensure_config()

        requested = key if key is not None else list(self._config.keys())
        configuration_key = []
        unknown_key = []

        for k in requested:
            if k in self._config:
                configuration_key.append({
                    "key": k,
                    "value": str(self._config[k]),
                    "readonly": False,  # se quiser simular chaves readonly, mude aqui
                })
            else:
                unknown_key.append(k)

        # ATENÇÃO: use a classe SEM “Payload”, para ficar igual ao resto do teu projeto
        return call_result.GetConfiguration(
            configuration_key=configuration_key,
            unknown_key=unknown_key or None
        )
    
    @on('ChangeConfiguration')
    async def on_change_configuration(self, key, value, **kwargs):
        self._ensure_config()

        if key not in self._config:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)

        # validações simples e sincronização entre chaves correlatas
        if key == "AuthorizeRequired":
            b = _as_bool(value)
            self._config["AuthorizeRequired"] = "true" if b else "false"
            self.authorize_required = b
            # mantém coerência com evb_SkipAuthorize (inverso)
            self._config["evb_SkipAuthorize"] = "0" if b else "1"

        elif key == "evb_SkipAuthorize":
            b = _as_bool(value)
            self._config["evb_SkipAuthorize"] = "1" if b else "0"
            self._config["AuthorizeRequired"] = "false" if b else "true"
            self.authorize_required = not b

        elif key in ("HeartbeatInterval", "MeterValuesSampleInterval"):
            if not str(value).isdigit():
                return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
            self._config[key] = str(value)

        else:
            # fallback: aceita e grava como string
            self._config[key] = str(value)

        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

# ---------------------------- GUI ---------------------------------

class EVChargerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVBox G3 Mock - OCPP 1.6 (GUI)")
        self.setGeometry(80, 80, 900, 620)

        self.cp: Optional[EVBoxG3Simulator] = None
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.power_w = 0
        self.energy_wh = 0

        self.id_tag = "DAIANE_EVB1234"

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # Configurações
        config_group = QGroupBox("Configuração")
        config_layout = QVBoxLayout()

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("ID do Carregador:"))
        self.charger_id_input = QLineEdit("EVB_G3_01")
        row1.addWidget(self.charger_id_input)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("ID Tag (idTag):"))
        self.rfid_input = QLineEdit(self.id_tag)
        row2.addWidget(self.rfid_input)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("URL do Servidor:"))
        self.server_url_input = QLineEdit("ws://172.18.3.132:9001/ws/")
        row3.addWidget(self.server_url_input)

        self.connect_button = QPushButton("Conectar")
        self.connect_button.clicked.connect(self.toggle_connection)

        config_layout.addLayout(row1)
        config_layout.addLayout(row2)
        config_layout.addLayout(row3)
        config_layout.addWidget(self.connect_button)
        config_group.setLayout(config_layout)
        main_layout.addWidget(config_group)

        # Status
        status_group = QGroupBox("Status")
        status_layout = QVBoxLayout()

        self.connection_status = QLabel("Desconectado")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        status_layout.addWidget(self.connection_status)

        self.charging_status = QLabel("Desconectado")
        self.charging_status.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.charging_status)

        energy_row = QHBoxLayout()
        energy_row.addWidget(QLabel("Potência Estimada:"))
        self.power_label = QLabel("0 W")
        energy_row.addWidget(self.power_label)

        energy_row.addWidget(QLabel("Energia Total (Wh):"))
        self.energy_label = QLabel("0")
        energy_row.addWidget(self.energy_label)

        status_layout.addLayout(energy_row)
        status_group.setLayout(status_layout)
        main_layout.addWidget(status_group)

        # Controles
        control_group = QGroupBox("Controles")
        control_layout = QHBoxLayout()

        self.start_button = QPushButton("Iniciar Carregamento")
        self.start_button.clicked.connect(self.start_charging)
        self.start_button.setEnabled(False)
        control_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Parar Carregamento")
        self.stop_button.clicked.connect(self.stop_charging)
        self.stop_button.setEnabled(False)
        control_layout.addWidget(self.stop_button)

        control_group.setLayout(control_layout)
        main_layout.addWidget(control_group)

        # Logs
        log_group = QGroupBox("Logs")
        log_layout = QVBoxLayout()
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        log_layout.addWidget(self.log_display)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group, 1)

        # Timers
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.update_ui)
        self.ui_timer.start(500)

        loop = asyncio.get_event_loop()
        loop.create_task(self.monitor_connection())

        self.log_message("✅ GUI iniciada. Configure e conecte ao servidor OCPP.")

    # ------------------------ GUI helpers -------------------------
    def log_message(self, message: str):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_display.append(f"[{timestamp}] {message}")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())

    def update_charging_status(self, status: str):
        self.charging_status.setText(status)
        if "Carregando" in status:
            self.charging_status.setStyleSheet("color: green; font-weight: bold;")
        elif "Disponível" in status:
            self.charging_status.setStyleSheet("color: blue; font-weight: bold;")
        elif "Desconectado" in status:
            self.charging_status.setStyleSheet("color: red; font-weight: bold;")
        else:
            self.charging_status.setStyleSheet("color: gray; font-weight: bold;")

    def update_energy_values(self, power_w: int, energy_wh: int):
        self.power_w = power_w
        self.energy_wh = energy_wh

    def update_ui(self):
        self.power_label.setText(f"{self.power_w} W")
        self.energy_label.setText(f"{self.energy_wh} Wh")
        if self.cp and self.cp.charging:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
        elif self.cp and self.cp.connected:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
        else:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)

    # --------------------- Conexão / Monitor ----------------------
    async def monitor_connection(self):
        while True:
            await asyncio.sleep(5)
            if self.cp and not self.cp.connected:
                self.log_message("🔄 Conexão perdida. Tentando reconectar…")
                self.connection_status.setText("Reconectando…")
                self.connection_status.setStyleSheet("color: orange; font-weight: bold;")
                try:
                    if self.websocket:
                        await self.websocket.close()
                except Exception:
                    pass
                try:
                    charger_id = self.charger_id_input.text().strip()
                    base_url = self.server_url_input.text().rstrip('/') + '/' + charger_id
                    self.websocket = await websockets.connect(base_url, subprotocols=["ocpp1.6"])
                    await self.cp.resume_connection(self.websocket)
                    self.connection_status.setText("Conectado")
                    self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                    self.log_message("✅ Reconectado com sucesso.")
                except Exception as e:
                    self.log_message(f"❌ Falha ao reconectar: {e}")

    async def async_connect_to_server(self):
        charger_id = self.charger_id_input.text().strip()
        server_url = self.server_url_input.text().rstrip('/') + '/' + charger_id
        while True:
            try:
                self.log_message(f"🌐 Conectando em {server_url}…")
                self.websocket = await websockets.connect(server_url, subprotocols=["ocpp1.6"])
                self.cp = EVBoxG3Simulator(charger_id, self.websocket, self)
                self.connection_status.setText("Conectado")
                self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                self.log_message("✅ Conectado ao servidor OCPP!")
                await self.cp.start()
                break
            except Exception as e:
                self.log_message(f"❌ Conexão falhou: {e}")
                self.connection_status.setText("Erro de conexão")
                self.connection_status.setStyleSheet("color: red; font-weight: bold;")
                try:
                    if self.websocket:
                        await self.websocket.close()
                except Exception:
                    pass
                await asyncio.sleep(5)

    def connect_to_server(self):
        loop = asyncio.get_event_loop()
        loop.create_task(self.async_connect_to_server())
        self.connect_button.setText("Desconectar")

    def disconnect_from_server(self):
        if self.cp:
            self.cp.charging = False
            loop = asyncio.get_event_loop()
            loop.create_task(self.websocket.close())
            self.cp = None
            self.websocket = None
            self.connection_status.setText("Desconectado")
            self.connection_status.setStyleSheet("color: red; font-weight: bold;")
            self.update_charging_status("Desconectado")
            self.log_message("🔌 Desconectado do servidor OCPP")
        self.connect_button.setText("Conectar")

    def toggle_connection(self):
        if self.cp:
            self.disconnect_from_server()
        else:
            self.connect_to_server()

    def start_charging(self):
        if self.cp and not self.cp.charging:
            id_tag = self.rfid_input.text().strip() or "EVB_TAG"
            self.id_tag = id_tag
            self.log_message(f"⚡ Iniciando sessão para tag: {id_tag}")
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.start_session_flow(id_tag))

    def stop_charging(self):
        if self.cp and self.cp.charging:
            self.log_message("🛑 Parando sessão…")
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.stop_session(reason="Local"))


# --------------------------- Entrypoint ----------------------------

def main():
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = EVChargerGUI()
    window.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
