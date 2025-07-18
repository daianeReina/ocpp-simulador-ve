import asyncio
import websockets
import logging
import sys
from datetime import datetime, timezone
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import RegistrationStatus, ChargePointStatus, AuthorizationStatus, ResetStatus
from ocpp.routing import on
from ocpp.messages import CallError
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QGroupBox, QLineEdit
)
from PyQt5.QtCore import QTimer
import qasync
import json

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class CP_Simulator(BaseChargePoint):
    def __init__(self, id, connection, gui):
        super().__init__(id, connection)
        self.id_tag = "ABC123456"
        self.transaction_id = None
        self.gui = gui
        self.charging = False
        self.heartbeat_interval = 30
        self.data_transfer_interval = 30  # Intervalo para DataTransfer (segundos)
        self.status = ChargePointStatus.available
        self.connector_id = 1
        self.connected = True

    async def start(self):
        # BootNotification inicial
        asyncio.create_task(super().start())
        try:
            self.gui.log_message("🔌 Enviando BootNotification inicial...")
            response = await self.call(call.BootNotification(
                charge_point_model="XPTO Charger",
                charge_point_vendor="Simulator",
                charge_point_serial_number="123456789TESTE",
                firmware_version="1.0.0"
            ))
            self.gui.log_message(f"✅ BootNotification aceito: {response.status}")
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar BootNotification: {e}")
            self.connected = False
            return

        # Inicia tarefas periódicas
        asyncio.create_task(self.periodic_heartbeat())
        asyncio.create_task(self.periodic_data_transfer())

    async def periodic_heartbeat(self):
        while self.connected:
            await asyncio.sleep(self.heartbeat_interval)
            if not self.charging and self.connected:
                await self.send_heartbeat()

    async def periodic_data_transfer(self):
        while self.connected:
            await asyncio.sleep(self.data_transfer_interval)
            if not self.charging and self.connected:
                # Exemplo de dados customizados
                payload = {"timestamp": datetime.utcnow().isoformat(), "status": self.status.name}
                await self.send_data_transfer(vendor_id="Simulator", message_id="PeriodicStatus", data=payload)

    async def send_heartbeat(self):
        try:
            await self.call(call.Heartbeat())
            self.gui.log_message("💓 Heartbeat enviado")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante heartbeat")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar heartbeat: {e}")
            self.connected = False

    async def send_data_transfer(self, vendor_id: str, message_id: str, data):
        """
        Envia uma requisição DataTransfer ao CSMS.
        """
        try:
            response = await self.call(call.DataTransfer(vendor_id=vendor_id, message_id=message_id, data=json.dumps(data)
            ))
            self.gui.log_message(f"📤 DataTransfer enviado: vendor_id={vendor_id}, message_id={message_id}, status={response.status}")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante DataTransfer")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar DataTransfer: {e}")

    @on('BootNotification')
    async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        self.gui.log_message(f"🔌 BootNotification recebido: {charge_point_model} - {charge_point_vendor}")
        return call_result.BootNotification(
            current_time=datetime.utcnow().isoformat(),
            interval=self.heartbeat_interval,
            status=RegistrationStatus.accepted
        )

    @on('Authorize')
    async def on_authorize(self, id_tag, **kwargs):
        self.gui.log_message(f"🔑 Authorize recebido para tag {id_tag}")
        return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

    @on('RemoteStartTransaction')
    async def on_remote_start_transaction(self, connector_id, id_tag, **kwargs):
        self.gui.log_message(f"⚡ RemoteStartTransaction recebido para tag {id_tag}")
        asyncio.create_task(self.start_charging_sequence(id_tag))
        return call_result.RemoteStartTransaction(status="Accepted")

    @on('RemoteStopTransaction')
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        self.gui.log_message(f"🛑 RemoteStopTransaction recebido para transação {transaction_id}")
        if self.charging and self.transaction_id == transaction_id:
            self.charging = False
            return call_result.RemoteStopTransaction(status="Accepted")
        return call_result.RemoteStopTransaction(status="Rejected")

    @on('Heartbeat')
    async def on_heartbeat(self, **kwargs):
        self.gui.log_message("💓 Heartbeat recebido")
        return call_result.Heartbeat(current_time=datetime.utcnow().isoformat())

    @on('StatusNotification')
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        self.gui.log_message(f"📊 StatusNotification: {status} (Erro: {error_code})")
        return call_result.StatusNotification()
    
    @on('DataTransfer')
    async def on_data_transfer(self, vendor_id, message_id, data, **kwargs):
        """
        Trata mensagens DataTransfer enviadas pelo CSMS.
        """
        self.gui.log_message(f"📲 DataTransfer recebido (vendor_id={vendor_id}, message_id={message_id}) com dados: {data}")
        # Responde aceitanto e ecoando os dados
        return call_result.DataTransfer(
            status="Accepted",
            data=data
        )

    
    async def start_charging_sequence(self, id_tag):
        if self.charging:
            self.gui.log_message("⚠️ Carregamento já em andamento.")
            return

        self.gui.log_message(f"🔐 Solicitando autorização para tag {id_tag}...")
        try:
            response = await self.call(call.Authorize(id_tag=id_tag))
            auth_status = response.id_tag_info["status"]
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao solicitar autorização: {e}")
            return

        if auth_status != AuthorizationStatus.accepted:
            self.gui.log_message(f"❌ Tag {id_tag} não autorizada ({auth_status})")
            self.update_charging_status("RFID não autorizado")
            return

        self.gui.log_message(f"✅ Tag {id_tag} autorizada!")

        self.charging = True
        self.gui.update_charging_status("Carregando...")
        await self.send_status_notification(ChargePointStatus.preparing)
        await asyncio.sleep(2)
        await self.send_start_transaction(id_tag)
        await self.send_status_notification(ChargePointStatus.charging)
        await self.simulate_meter_values()
        if self.charging:
            await self.send_stop_transaction(reason="Local")
            await self.send_status_notification(ChargePointStatus.available)
            self.gui.update_charging_status("Disponível")
        self.charging = False


    async def send_status_notification(self, status):
        request = call.StatusNotification(
            connector_id=self.connector_id,
            error_code="NoError",
            status=status,
            timestamp=datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        )
        try:
            await self.call(request)
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante envio de status")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar status: {e}")

    async def send_start_transaction(self, id_tag):
        request = call.StartTransaction(
            connector_id=self.connector_id,
            id_tag=id_tag,
            meter_start=0,
            timestamp=datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        )
        try:
            response = await self.call(request)
            self.transaction_id = response.transaction_id
            self.gui.log_message(f"📝 Transação iniciada com ID: {self.transaction_id}")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante início de transação")
            self.connected = False
            self.charging = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao iniciar transação: {e}")
            self.charging = False

    async def simulate_meter_values(self):
        total_energy = 0
        for i in range(1, 11):
            if not self.charging or not self.connected:
                break
            await asyncio.sleep(3)
            energy = i * 150
            total_energy += energy
            request = call.MeterValues(
                connector_id=self.connector_id,
                transaction_id=self.transaction_id,
                meter_value=[{
                    "timestamp": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
                    "sampledValue": [
                        {"value": str(energy), "unit": "W", "measurand": "Power.Active.Import", "context": "Sample.Periodic"},
                        {"value": str(total_energy), "unit": "Wh", "measurand": "Energy.Active.Import.Register", "context": "Transaction.Begin"}
                    ]
                }]
            )
            try:
                await self.call(request)
                self.gui.log_message(f"🔋 Medição enviada: {energy}W (Total: {total_energy}Wh)")
                self.gui.update_energy_values(energy, total_energy)
            except websockets.exceptions.ConnectionClosed:
                self.gui.log_message("❌ Conexão fechada durante envio de medição")
                self.connected = False
                break
            except Exception as e:
                self.gui.log_message(f"❌ Erro ao enviar medição: {e}")
                break

    async def send_stop_transaction(self, reason="Remote"):
        """
        Encerra a transação de carregamento e envia StopTransaction.
        """
        request = call.StopTransaction(
            transaction_id=self.transaction_id,
            id_tag=self.id_tag,
            meter_stop=0,
            timestamp=datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
            reason=reason
        )
        try:
            await self.call(request)
            self.transaction_id = None
            self.gui.log_message("✅ Transação encerrada com sucesso")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante parada de transação")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao parar transação: {e}")

    async def resume_connection(self, new_connection):
        """
        Reconecta sem reenviar BootNotification:
        atualiza a conexão interna do BaseChargePoint e retoma o heartbeat.
        """
        self._connection = new_connection
        self.connected = True
        self.gui.log_message("🔄 Reconectado ao servidor, reiniciando listener e retomando heartbeat...")
        asyncio.create_task(super().start())
        asyncio.create_task(self.periodic_heartbeat())

class EVChargerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Simulador de Carregador VE - OCPP 1.6")
        self.setGeometry(100, 100, 800, 600)

        self.cp = None
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
        self.charger_id_input = QLineEdit("CP_SIMULATOR01")
        id_layout.addWidget(self.charger_id_input)
        rfid_layout = QHBoxLayout()
        rfid_layout.addWidget(QLabel("ID da Tag RFID:"))
        self.rfid_input = QLineEdit("DAIANE01")
        rfid_layout.addWidget(self.rfid_input)
        config_layout.addLayout(rfid_layout)

        config_layout.addLayout(id_layout)
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("URL do Servidor:"))
        self.server_url_input = QLineEdit("ws://172.18.3.132:9000/ws/")
        url_layout.addWidget(self.server_url_input)
        config_layout.addLayout(url_layout)
        self.connect_button = QPushButton("Conectar")
        self.connect_button.clicked.connect(self.toggle_connection)
        config_layout.addWidget(self.connect_button)
        config_group.setLayout(config_layout)
        main_layout.addWidget(config_group)
        self.id_tag = "DAIANE01"
        

        # Status
        status_group = QGroupBox("Status do Carregador")
        status_layout = QVBoxLayout()
        self.connection_status = QLabel("Desconectado")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        status_layout.addWidget(self.connection_status)
        self.charging_status = QLabel("Desconectado")
        self.charging_status.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.charging_status)
        energy_layout = QHBoxLayout()
        energy_layout.addWidget(QLabel("Potência Atual:"))
        self.power_label = QLabel("0 W")
        energy_layout.addWidget(self.power_label)
        energy_layout.addWidget(QLabel("Energia Total:"))
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

        self.log_message("✅ Aplicação iniciada. Configure e conecte ao servidor OCPP.")

    def log_message(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_display.append(f"[{timestamp}] {message}")
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

    async def monitor_connection(self):
        while True:
            await asyncio.sleep(5)
            if self.cp and not self.cp.connected:
                self.log_message("🔄 Conexão perdida. Tentando reconectar sem BootNotification...")
                self.connection_status.setText("Reconectando...")
                self.connection_status.setStyleSheet("color: orange; font-weight: bold;")
                try:
                    await self.websocket.close()
                except:
                    pass
                charger_id = self.charger_id_input.text()
                server_url = self.server_url_input.text().rstrip('/') + '/' + charger_id
                try:
                    self.websocket = await websockets.connect(server_url, subprotocols=["ocpp1.6"])
                    await self.cp.resume_connection(self.websocket)
                    self.connection_status.setText("Conectado")
                    self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                    self.log_message("✅ Reconectado com sucesso sem BootNotification.")
                except Exception as e:
                    self.log_message(f"❌ Falha ao reconectar: {e}")

    async def async_connect_to_server(self):
        charger_id = self.charger_id_input.text()
        server_url = self.server_url_input.text().rstrip('/') + '/' + charger_id
        while True:
            try:
                self.log_message(f"🌐 Tentando conectar em {server_url}...")
                self.websocket = await websockets.connect(server_url, subprotocols=["ocpp1.6"])
                self.cp = CP_Simulator(charger_id, self.websocket, self)
                self.connection_status.setText("Conectado")
                self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                self.log_message("✅ Conectado com sucesso ao servidor OCPP!")
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
            loop.create_task(self.cp.send_status_notification(ChargePointStatus.unavailable))
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
            id_tag = self.rfid_input.text()
            self.log_message(f"⚡ Iniciando carregamento para tag: {id_tag}")
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.start_charging_sequence(id_tag))

    def stop_charging(self):
        if self.cp and self.cp.charging:
            self.log_message("🛑 Parando carregamento localmente...")
            self.cp.charging = False
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.send_stop_transaction(reason="Local"))
            loop.create_task(self.cp.send_status_notification(ChargePointStatus.available))
            self.update_charging_status("Disponível")


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
