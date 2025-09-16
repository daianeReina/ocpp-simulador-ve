# evbox_mock.py
# Mock de carregador EV-BOX (OCPP 1.6J) com GUI PyQt5
# - Baseado no teu simulador (estrutura/GUI)
# - Adaptações para EV-BOX: vendor "EV-BOX", DataTransfer (evbFunctions), Connectors/ConnectionInfo pós-boot
# Requisitos: websockets, ocpp, PyQt5, qasync
# Execução: python evbox_mock.py

import asyncio
import websockets
import logging
import sys
from datetime import datetime, timezone
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    RegistrationStatus,
    ChargePointStatus,
    AuthorizationStatus,
    ResetStatus,
    ConfigurationStatus,
    DataTransferStatus,
)
from ocpp.routing import on
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QGroupBox, QLineEdit
)
from PyQt5.QtCore import QTimer
import qasync
import json
import random

# ---------------------------------------------
# Logging
# ---------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ---------------------------------------------
# Utilidades (tempo / helpers)
# ---------------------------------------------
def utcnow_iso():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


# ---------------------------------------------
# EVBox Mock (ChargePoint)
# ---------------------------------------------
class EVBoxMock(BaseChargePoint):
    """
    Mock de um carregador EV-BOX (OCPP 1.6J).
    - Envia BootNotification com vendor "EV-BOX" e model "Elvi".
    - Aceita RemoteStart/Stop, Reset, Change/GetConfiguration.
    - Suporta DataTransfer com messageIds evb* (caderno de funções).
    """

    def __init__(self, id, connection, gui):
        super().__init__(id, connection)
        # Estado interno
        self.gui = gui
        self.connected = True

        self.status = ChargePointStatus.available
        self.connector_id = 1

        # Sessão
        self.id_tag_default = "ABC123456"
        self.current_id_tag = None
        self.transaction_id = None
        self.charging = False

        # Medidor
        self.meter_wh_counter = 0
        self.meter_start_wh = 0

        # Config OCPP
        self.heartbeat_interval = 900
        self.meter_values_interval = 30
        # EVB / AutoStart
        # AuthorizeRequired=True => Autostart OFF
        self.authorize_required = True

        # Simulação de “rede” para evbConnectionInfo
        self.current_connection = "Wi-Fi"   # "Wi-Fi" | "Cellular" | "none"
        self.wifi_connected = True
        self.cell_connected = False
        self.wifi_rssi = -50
        self.cell_rssi = -95

    # ---------------------------
    # Ciclo de vida
    # ---------------------------
    async def start(self):
        asyncio.create_task(super().start())
        try:
            self.gui.log_message("🔌 Enviando BootNotification (EV-BOX/Elvi)...")
            resp = await self.call(call.BootNotification(
                charge_point_model="Elvi",
                charge_point_vendor="EV-BOX",
                # Estes campos adicionais alguns stacks ignoram, ajudam a depurar
                charge_point_serial_number="EVB-MOCK-0001",
                firmware_version="1.0.0-mock"
            ))
            self.heartbeat_interval = getattr(resp, "interval", 900) or 900
            if getattr(resp, "status", RegistrationStatus.accepted) != RegistrationStatus.accepted:
                raise RuntimeError("Boot rejeitado pelo CSMS.")
            self.gui.log_message(f"✅ BootNotification aceito: interval={self.heartbeat_interval}s")
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar BootNotification: {e}")
            self.connected = False
            return

        # Tarefas periódicas
        asyncio.create_task(self.periodic_heartbeat())

        # EVB “caderno de funções”:
        # - ConnectorsNotification após Boot.
        # - ConnectionInfo ~20s após Boot (apressamos para 5s por conveniência).
        asyncio.create_task(self._post_boot_notifications())

    async def _post_boot_notifications(self):
        # evbConnectorsNotification imediatamente após boot
        await asyncio.sleep(1)
        await self.send_evb_connectors_notification()

        # evbConnectionInfo ~5s após boot (manual sugere ~20s)
        await asyncio.sleep(5)
        await self.send_evb_connection_info()

    async def periodic_heartbeat(self):
        while self.connected:
            await asyncio.sleep(self.heartbeat_interval)
            if self.connected:
                try:
                    await self.call(call.Heartbeat())
                    self.gui.log_message("💓 Heartbeat enviado")
                except websockets.exceptions.ConnectionClosed:
                    self.gui.log_message("❌ Conexão fechada durante heartbeat")
                    self.connected = False
                except Exception as e:
                    self.gui.log_message(f"❌ Erro no heartbeat: {e}")
                    self.connected = False

    # ---------------------------
    # Envio de DataTransfer (EVB)
    # ---------------------------
    async def send_evb_connectors_notification(self):
        """
        Envia evbConnectorsNotification (pós-boot).
        Estrutura simplificada (JSON) para debug. Pode-se adaptar ao formato “compacto”.
        """
        data = {
            "connector": [{
                "connectorId": self.connector_id,
                "serialNumber": "G4SN001",
                "firmwareVersion": "0414",
                "cableType": "T2S",          # Tipo fictício
                "meterType": "GenericMeter",
                "meterFirmwareVersion": "1.0",
                "meterSerialNumber": "MTR-EVB-001",
                "meterFrequency": 5000,      # 50.00 Hz (1/100 Hz)
                "meterPhases": "L1L2L3",
                "hardwareId": 414            # G4
            }]
        }
        await self._send_evb("evbConnectorsNotification", data)
        self.gui.log_message("🔔 evbConnectorsNotification enviado")

    async def send_evb_connection_info(self):
        """
        Envia evbConnectionInfo cerca de 5s após o Boot (para mock).
        """
        data = {
            "currentConnection": self.current_connection,
            "wifiConnection": {
                "available": True,
                "configured": True,
                "network": self.wifi_connected,
                "rssi": self.wifi_rssi,
                "lastOnline": 3
            },
            "cellConnection": {
                "available": True,
                "simCard": True,
                "configured": True,
                "network": self.cell_connected,
                "rssi": self.cell_rssi,
                "lastOnline": 999
            }
        }
        await self._send_evb("evbConnectionInfo", data)
        self.gui.log_message("📡 evbConnectionInfo enviado")

    async def send_evb_status_notification_expanded(self):
        """
        Opcional: envia um status expandido (evbStatusNotification).
        """
        data = {
            "connectorId": self.connector_id,
            "status": self.status.name,
            "errorCode": "NoError",
            "vendorErrorCode": "No error.",
            "charging": "1" if self.charging else "0",
            "leds": "Green" if self.charging else "Blue",
            "meter": {"value": self.meter_wh_counter, "power": 0},
            "timestamp": utcnow_iso(),
            "transactionId": self.transaction_id or 0
        }
        await self._send_evb("evbStatusNotification", data)
        self.gui.log_message("📊 evbStatusNotification enviado")

    async def _send_evb(self, message_id: str, data: dict):
        try:
            resp = await self.call(call.DataTransfer(
                vendor_id="EV-BOX",
                message_id=message_id,
                data=json.dumps(data)
            ))
            self.gui.log_message(f"📤 DataTransfer '{message_id}' → status={resp.status}")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante DataTransfer")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar DataTransfer '{message_id}': {e}")

    # ---------------------------
    # Handlers OCPP ← CSMS
    # ---------------------------
    @on('BootNotification')
    async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        self.gui.log_message(f"🔌 (CSMS→CP) BootNotification recebido: {charge_point_model} - {charge_point_vendor}")
        return call_result.BootNotification(
            current_time=utcnow_iso(),
            interval=self.heartbeat_interval,
            status=RegistrationStatus.accepted
        )

    @on('Authorize')
    async def on_authorize(self, id_tag, **kwargs):
        # Mock: aceita todas (ou respeita AutoStart)
        self.gui.log_message(f"🔑 Authorize recebido para tag {id_tag}")
        return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

    @on('RemoteStartTransaction')
    async def on_remote_start_transaction(self, connector_id, id_tag, **kwargs):
        self.gui.log_message(f"⚡ RemoteStartTransaction recebido para tag {id_tag}")
        asyncio.create_task(self.start_charging_sequence(id_tag))
        # Em OCPP 1.6, a resposta é payload com status Accepted/Rejected
        return call_result.RemoteStartTransactionPayload(status="Accepted")

    @on('RemoteStopTransaction')
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        self.gui.log_message(f"🛑 RemoteStopTransaction recebido p/ transação {transaction_id}")
        if self.charging and self.transaction_id == transaction_id:
            self.charging = False
            return call_result.RemoteStopTransactionPayload(status="Accepted")
        return call_result.RemoteStopTransactionPayload(status="Rejected")

    @on('Reset')
    async def on_reset(self, type, **kwargs):
        self.gui.log_message(f"🔄 Reset recebido ({type})")
        # Simula “reset suave”: não encerra transação ativa se quisermos imitar evb_ResumeAfterReset
        return call_result.Reset(status=ResetStatus.accepted)

    @on('GetConfiguration')
    async def on_get_configuration(self, key=None, **kwargs):
        """
        Fornece um pequeno conjunto de chaves (incluindo o par AuthorizeRequired/AutoStart).
        """
        authorize_required = getattr(self, "authorize_required", True)

        store = {
            "AuthorizeRequired": ("true" if authorize_required else "false", False),
            "MeterValuesSampleInterval": (str(self.meter_values_interval), False),
            "HeartbeatInterval": (str(self.heartbeat_interval), False),
            # Mapeamento EVB comum
            "evb_AutoStart": ("0" if authorize_required else "1", False),
        }

        requested = key or list(store.keys())
        configuration_key, unknown_key = [], []

        for k in requested:
            if k in store:
                val, ro = store[k]
                configuration_key.append({"key": k, "value": val, "readonly": ro})
            else:
                unknown_key.append(k)

        return call_result.GetConfigurationPayload(
            configuration_key=configuration_key,
            unknown_key=unknown_key or None
        )

    @on('ChangeConfiguration')
    async def on_change_configuration(self, key, value, **kwargs):
        """
        Aceita alterações básicas e sincroniza AutoStart/AuthorizeRequired.
        """
        if key == "AuthorizeRequired":
            self.authorize_required = str(value).lower() == "true"
            self.gui.update_autostart_status(not self.authorize_required)
            self.gui.log_message(f"🔧 AuthorizeRequired = {self.authorize_required}")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

        if key == "evb_AutoStart":
            # "1" → AutoStart ON → AuthorizeRequired = False
            autostart_on = str(value) in ("1", "true", "yes")
            self.authorize_required = not autostart_on
            self.gui.update_autostart_status(autostart_on)
            self.gui.log_message(f"🔧 evb_AutoStart = {value} → AuthorizeRequired = {self.authorize_required}")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

        if key == "HeartbeatInterval":
            try:
                self.heartbeat_interval = int(value)
                return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
            except Exception:
                return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)

        if key == "MeterValuesSampleInterval":
            try:
                self.meter_values_interval = max(1, int(value))
                return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
            except Exception:
                return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)

        self.gui.log_message(f"⚠️ Chave não suportada: {key}")
        return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)

    @on('DataTransfer')
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        """
        Trata comandos EVB enviados pelo CSMS via DataTransfer.
        - Apenas valida/atualiza estado e responde Accepted.
        - Dados chegam tipicamente como string; tentamos parsear JSON para debug amigável.
        """
        self.gui.log_message(f"📲 DataTransfer recebido (vendor_id={vendor_id}, message_id={message_id})")
        parsed = None
        if data:
            try:
                parsed = json.loads(data)
            except Exception:
                parsed = {"_raw": data}

        # Ações comuns do “caderno” (tratamento básico)
        if vendor_id == "EV-BOX":
            if message_id in ("evbServerGet", "evbServerSet"):
                # Apenas ecoa/aceita
                self.gui.log_message("🌐 evbServer* (mock) OK")
            elif message_id in ("evbConfigGlobalGet", "evbConfigGlobalSet"):
                # Poderíamos atualizar flags (autoStart/skipAuthorize, etc.)
                self.gui.log_message("⚙️ evbConfigGlobal* (mock) OK")
            elif message_id == "evbConnectorsGet":
                # Responderíamos com a lista; aqui só aceitamos
                self.gui.log_message("🔌 evbConnectorsGet (mock) OK")
            elif message_id == "evbStatusNotification":
                self.gui.log_message("📊 evbStatusNotification (mock) OK")
            elif message_id == "evbConnectionInfo":
                self.gui.log_message("📡 evbConnectionInfo (mock) OK")
            # Outros messageIds podem ser acrescentados conforme necessário.

        # Resposta: Accepted + eco (para debug)
        return call_result.DataTransfer(
            status=DataTransferStatus.accepted,
            data=json.dumps({"ack": True, "received": {"vendor_id": vendor_id, "message_id": message_id, "data": parsed}})
        )

    # ---------------------------
    # Sequência de carregamento
    # ---------------------------
    async def start_charging_sequence(self, id_tag):
        if self.charging:
            self.gui.log_message("⚠️ Carregamento já em andamento.")
            return

        # AutoStart
        if hasattr(self, "authorize_required") and not self.authorize_required:
            self.gui.log_message("✅ AutoStart ativo — sem pedir Authorize.")
            auth_status = AuthorizationStatus.accepted
            id_tag = "autostart"
        else:
            self.gui.log_message(f"🔐 Solicitando Authorize para {id_tag}...")
            try:
                response = await self.call(call.Authorize(id_tag=id_tag))
                auth_status = response.id_tag_info["status"]
            except Exception as e:
                self.gui.log_message(f"❌ Falha no Authorize: {e}")
                return

            if auth_status != AuthorizationStatus.accepted:
                self.gui.log_message(f"❌ Tag {id_tag} rejeitada ({auth_status})")
                self.gui.update_charging_status("RFID não autorizado")
                return
            self.gui.log_message(f"✅ Tag {id_tag} autorizada!")

        # Preparação
        self.charging = True
        self.gui.update_charging_status("Carregando...")
        await self._send_status(ChargePointStatus.preparing)
        await asyncio.sleep(1.5)

        # StartTransaction (usa o contador real do medidor)
        await self._send_start_transaction(id_tag)

        # Status Charging + ciclos de MeterValues
        await self._send_status(ChargePointStatus.charging)
        await self._simulate_meter_values()

        if self.charging:
            # Amostra final (Transaction.End) opcional + StopTransaction
            await self._send_transaction_end_sample()
            await self._send_stop_transaction(reason="Local")
            await self._send_status(ChargePointStatus.available)
            self.gui.update_charging_status("Disponível")

        self.charging = False

    async def _send_status(self, status):
        self.status = status
        req = call.StatusNotification(
            connector_id=self.connector_id,
            error_code="NoError",
            status=status,
            timestamp=utcnow_iso()
        )
        try:
            await self.call(req)
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar StatusNotification: {e}")

    async def _send_start_transaction(self, id_tag):
        self.current_id_tag = id_tag
        self.meter_start_wh = self.meter_wh_counter

        req = call.StartTransaction(
            connector_id=self.connector_id,
            id_tag=id_tag,
            meter_start=self.meter_start_wh,
            timestamp=utcnow_iso()
        )
        try:
            resp = await self.call(req)
            self.transaction_id = resp.transaction_id
            self.gui.log_message(
                f"📝 StartTransaction OK — tx_id={self.transaction_id} (meter_start={self.meter_start_wh} Wh)"
            )
        except Exception as e:
            self.gui.log_message(f"❌ Erro no StartTransaction: {e}")
            self.charging = False

    async def _simulate_meter_values(self):
        # 10 amostras com passo de 3s
        for i in range(1, 11):
            if not self.charging or not self.connected:
                break
            await asyncio.sleep(3)

            power_w = i * 150
            step_wh = max(1, int(round(power_w * 3 / 3600)))  # 3s → Wh
            self.meter_wh_counter += step_wh

            req = call.MeterValues(
                connector_id=self.connector_id,
                transaction_id=self.transaction_id,
                meter_value=[{
                    "timestamp": utcnow_iso(),
                    "sampledValue": [
                        {
                            "value": str(power_w),
                            "unit": "W",
                            "measurand": "Power.Active.Import",
                            "context": "Sample.Periodic"
                        },
                        {
                            "value": str(self.meter_wh_counter),
                            "unit": "Wh",
                            "measurand": "Energy.Active.Import.Register",
                            "context": "Sample.Periodic"
                        }
                    ]
                }]
            )
            try:
                await self.call(req)
                self.gui.log_message(
                    f"🔋 MeterValues: {power_w}W | meter={self.meter_wh_counter}Wh (+{step_wh}Wh)"
                )
                self.gui.update_energy_values(power_w, self.meter_wh_counter)
            except Exception as e:
                self.gui.log_message(f"❌ MeterValues falhou: {e}")
                break

    async def _send_transaction_end_sample(self):
        try:
            await self.call(call.MeterValues(
                connector_id=self.connector_id,
                transaction_id=self.transaction_id,
                meter_value=[{
                    "timestamp": utcnow_iso(),
                    "sampledValue": [
                        {
                            "value": str(self.meter_wh_counter),
                            "unit": "Wh",
                            "measurand": "Energy.Active.Import.Register",
                            "context": "Transaction.End"
                        }
                    ]
                }]
            ))
        except Exception:
            pass

    async def _send_stop_transaction(self, reason="Remote"):
        req = call.StopTransaction(
            transaction_id=self.transaction_id,
            id_tag=self.current_id_tag or self.id_tag_default,
            meter_stop=self.meter_wh_counter,
            timestamp=utcnow_iso(),
            reason=reason
        )
        try:
            await self.call(req)
            consumo = self.meter_wh_counter - self.meter_start_wh
            self.gui.log_message(
                f"✅ StopTransaction OK — start={self.meter_start_wh}Wh, stop={self.meter_wh_counter}Wh, consumo={consumo}Wh"
            )
            self.transaction_id = None
            self.current_id_tag = None
        except Exception as e:
            self.gui.log_message(f"❌ Erro no StopTransaction: {e}")

    async def resume_connection(self, new_connection):
        """
        Reconecta sem reenviar BootNotification (mantém sessão).
        """
        self._connection = new_connection
        self.connected = True
        self.gui.log_message("🔄 Reconectado — retomando heartbeat...")
        asyncio.create_task(super().start())
        asyncio.create_task(self.periodic_heartbeat())


# ---------------------------------------------
# GUI
# ---------------------------------------------
class EVChargerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EV-BOX Mock (OCPP 1.6)")
        self.setGeometry(100, 100, 860, 640)

        self.cp: EVBoxMock | None = None
        self.websocket = None
        self.power_w = 0
        self.energy_wh = 0

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # Configurações
        config_group = QGroupBox("Configuração do Carregador")
        config_layout = QVBoxLayout()
        id_layout = QHBoxLayout()
        id_layout.addWidget(QLabel("ID do Carregador:"))
        self.charger_id_input = QLineEdit("EVB_MOCK_01")
        id_layout.addWidget(self.charger_id_input)

        rfid_layout = QHBoxLayout()
        rfid_layout.addWidget(QLabel("RFID (idTag):"))
        self.rfid_input = QLineEdit("DAIANE01")
        rfid_layout.addWidget(self.rfid_input)

        config_layout.addLayout(rfid_layout)
        config_layout.addLayout(id_layout)

        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("URL do Servidor:"))
        self.server_url_input = QLineEdit("ws://127.0.0.1:9000/ws/")
        url_layout.addWidget(self.server_url_input)
        config_layout.addLayout(url_layout)

        self.connect_button = QPushButton("Conectar")
        self.connect_button.clicked.connect(self.toggle_connection)
        config_layout.addWidget(self.connect_button)

        config_group.setLayout(config_layout)
        main_layout.addWidget(config_group)

        # Status
        status_group = QGroupBox("Status do Carregador")
        status_layout = QVBoxLayout()
        self.connection_status = QLabel("Desconectado")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        status_layout.addWidget(self.connection_status)
        self.charging_status = QLabel("Desconectado")
        self.charging_status.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.charging_status)

        self.autostart_status = QLabel("AutoStart: Desconhecido")
        self.autostart_status.setStyleSheet("color: orange; font-weight: bold;")
        status_layout.addWidget(self.autostart_status)

        energy_layout = QHBoxLayout()
        energy_layout.addWidget(QLabel("Potência Atual:"))
        self.power_label = QLabel("0 W")
        energy_layout.addWidget(self.power_label)
        energy_layout.addWidget(QLabel("Energia Total (medidor):"))
        self.energy_label = QLabel("0 Wh")
        energy_layout.addWidget(self.energy_label)
        status_layout.addLayout(energy_layout)

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

        # Botões EVB
        self.btn_evb_conn_info = QPushButton("Enviar evbConnectionInfo")
        self.btn_evb_conn_info.clicked.connect(self._send_conn_info_clicked)
        control_layout.addWidget(self.btn_evb_conn_info)

        self.btn_evb_connectors = QPushButton("Enviar evbConnectorsNotification")
        self.btn_evb_connectors.clicked.connect(self._send_connectors_clicked)
        control_layout.addWidget(self.btn_evb_connectors)

        control_group.setLayout(control_layout)
        main_layout.addWidget(control_group)

        # Logs
        log_group = QGroupBox("Log de Mensagens")
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

        self.log_message("✅ Iniciado. Configura e conecta ao teu CSMS OCPP.")

    def log_message(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"[{ts}] {message}")
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())

    def update_charging_status(self, status):
        self.charging_status.setText(status)
        if "Carregando" in status:
            self.charging_status.setStyleSheet("color: green; font-weight: bold;")
        elif "Disponível" in status:
            self.charging_status.setStyleSheet("color: blue; font-weight: bold;")
        else:
            self.charging_status.setStyleSheet("color: gray; font-weight: bold;")

    def update_energy_values(self, power_w, energy_wh):
        self.power_w = power_w
        self.energy_wh = energy_wh

    def update_ui(self):
        self.power_label.setText(f"{self.power_w} W")
        self.energy_label.setText(f"{self.energy_wh} Wh")
        if self.cp and self.cp.charging:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
        elif self.cp and self.cp.status == ChargePointStatus.available:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
        else:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)

    def update_autostart_status(self, enabled: bool):
        if enabled:
            self.autostart_status.setText("AutoStart: Ativo")
            self.autostart_status.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.autostart_status.setText("AutoStart: Inativo")
            self.autostart_status.setStyleSheet("color: red; font-weight: bold;")

    async def monitor_connection(self):
        while True:
            await asyncio.sleep(5)
            if self.cp and not self.cp.connected:
                self.log_message("🔄 Conexão perdida. Tentando reconectar sem Boot...")
                self.connection_status.setText("Reconectando...")
                self.connection_status.setStyleSheet("color: orange; font-weight: bold;")
                try:
                    await self.websocket.close()
                except:
                    pass
                charger_id = self.charger_id_input.text().strip()
                server_url = self.server_url_input.text().rstrip('/') + '/' + charger_id
                try:
                    self.websocket = await websockets.connect(server_url, subprotocols=["ocpp1.6"])
                    await self.cp.resume_connection(self.websocket)
                    self.connection_status.setText("Conectado")
                    self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                    self.log_message("✅ Reconectado com sucesso sem Boot.")
                except Exception as e:
                    self.log_message(f"❌ Falha ao reconectar: {e}")

    async def async_connect_to_server(self):
        charger_id = self.charger_id_input.text().strip()
        server_url = self.server_url_input.text().rstrip('/') + '/' + charger_id
        while True:
            try:
                self.log_message(f"🌐 Conectando em {server_url}...")
                self.websocket = await websockets.connect(server_url, subprotocols=["ocpp1.6"])
                self.cp = EVBoxMock(charger_id, self.websocket, self)
                self.connection_status.setText("Conectado")
                self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                self.log_message("✅ Conectado ao CSMS!")
                await self.cp.start()
                break
            except Exception as e:
                self.log_message(f"❌ Falha na conexão: {e}")
                self.connection_status.setText("Erro de conexão")
                self.connection_status.setStyleSheet("color: red; font-weight: bold;")
                try:
                    await self.websocket.close()
                except:
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
            loop.create_task(self.cp._send_status(ChargePointStatus.unavailable))
            loop.create_task(self.websocket.close())
            self.cp = None
            self.websocket = None
            self.connection_status.setText("Desconectado")
            self.connection_status.setStyleSheet("color: red; font-weight: bold;")
            self.update_charging_status("Desconectado")
            self.log_message("🔌 Desconectado do CSMS")
        self.connect_button.setText("Conectar")

    def toggle_connection(self):
        if self.cp:
            self.disconnect_from_server()
        else:
            self.connect_to_server()

    def start_charging(self):
        if self.cp and not self.cp.charging:
            id_tag = self.rfid_input.text().strip() or "DAIANE01"
            self.log_message(f"⚡ Iniciando carregamento para tag: {id_tag}")
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.start_charging_sequence(id_tag))

    def stop_charging(self):
        if self.cp and self.cp.charging:
            self.log_message("🛑 Parando carregamento localmente...")
            self.cp.charging = False
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp._send_transaction_end_sample())
            loop.create_task(self.cp._send_stop_transaction(reason="Local"))
            loop.create_task(self.cp._send_status(ChargePointStatus.available))
            self.update_charging_status("Disponível")

    # Botões EVB
    def _send_conn_info_clicked(self):
        if self.cp:
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.send_evb_connection_info())

    def _send_connectors_clicked(self):
        if self.cp:
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.send_evb_connectors_notification())


# ---------------------------------------------
# Main
# ---------------------------------------------
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
