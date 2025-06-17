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

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class CP_Simulator(BaseChargePoint):
    def __init__(self, id, connection, gui):
        super().__init__(id, connection)
        self.id_tag = "ABC123456"
        self.transaction_id = None
        self.gui = gui
        self.charging = False
        self.heartbeat_interval = 60
        self.status = ChargePointStatus.available
        self.connector_id = 1
        self.connected = True  # Flag para controlar o estado da conexão

    async def start(self):
        # Envia o BootNotification inicial
        asyncio.create_task(super().start())

        try:
            self.gui.log_message("🔌 Enviando BootNotification inicial...")
            response = await self.call(call.BootNotification(
                charge_point_model="XPTO Charger",
                charge_point_vendor="Simulator",
                firmware_version="1.0.0"
            ))
            self.gui.log_message(f"✅ BootNotification aceito: {response.status}")
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar BootNotification: {e}")
            self.connected = False
            return


        # Inicia tarefa de heartbeat periódico
        asyncio.create_task(self.periodic_heartbeat())
        

    async def periodic_heartbeat(self):
        while self.connected:
            await asyncio.sleep(self.heartbeat_interval)
            if not self.charging and self.connected:
                await self.send_heartbeat()

    async def send_heartbeat(self):
        try:
            request = call.Heartbeat()
            await self.call(request)
            self.gui.log_message("💓 Heartbeat enviado")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante heartbeat")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao enviar heartbeat: {str(e)}")
            self.connected = False

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
        return call_result.Authorize(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

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
    
    @on('Reset')
    async def on_reset(self, type, **kwargs):
        agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.gui.log_message(f"🔄 Reset recebido (tipo: {type}) às {agora}")

        # Agenda o “reboot” em background, sem bloquear o on_reset
        asyncio.get_event_loop().create_task(self._simulate_reboot())

        # Responde imediatamente ao CSMS
        return call_result.Reset(status=ResetStatus.accepted)

    async def _simulate_reboot(self):
        # Pequena pausa para simular boot
        await asyncio.sleep(1)

        # Agora sim, envie o BootNotification como CALL, esperando resposta
        self.gui.log_message("🔌 Enviando BootNotification pós-reset…")
        try:
            response = await self.call(call.BootNotification(
                charge_point_model="Simulator",
                charge_point_vendor="Example",
                firmware_version="1.0.0"
            ))
            self.gui.log_message(f"✅ BootNotification aceito: {response.status}")
        except Exception as e:
            self.gui.log_message(f"❌ Falha no BootNotification pós-reset: {e}")
            
    @on('*')
    async def on_any_other_action(self, **kwargs):
        self.gui.log_message("⚠️ Ação não suportada recebida")
        return CallError(unique_id="unknown", error_code="NotSupported", error_description="Ação não suportada")

    async def start_charging_sequence(self, id_tag):
        if self.charging:
            self.gui.log_message("⚠️ Carregamento já em andamento. Ignorando novo pedido.")
            return
            
        self.charging = True
        self.gui.update_charging_status("Carregando...")
        
        # Envia status de preparação
        await self.send_status_notification(ChargePointStatus.preparing)
        await asyncio.sleep(2)
        
        # Inicia transação
        await self.send_start_transaction(id_tag)
        await self.send_status_notification(ChargePointStatus.charging)
        
        # Simula valores de medidor
        await self.simulate_meter_values()
        
        # Se ainda está carregando, encerra a transação
        if self.charging:
            # Usando "Local" para término automático
            await self.send_stop_transaction(reason="Local")
            await self.send_status_notification(ChargePointStatus.available)
            self.gui.update_charging_status("Disponível")
        
        self.charging = False

    async def send_status_notification(self, status):
        self.status = status
        self.gui.log_message(f"📊 Enviando StatusNotification: {status.value}")
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
            self.gui.log_message(f"❌ Erro ao enviar status: {str(e)}")

    async def send_start_transaction(self, id_tag):
        self.gui.log_message("🚗 Iniciando transação de carregamento")
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
            self.gui.log_message(f"❌ Erro ao iniciar transação: {str(e)}")
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
                self.gui.log_message(f"❌ Erro ao enviar medição: {str(e)}")
                break

    async def send_stop_transaction(self, reason="Remote"):
        self.gui.log_message(f"🛑 Encerrando transação de carregamento (Motivo: {reason})")
        request = call.StopTransaction(
            transaction_id=self.transaction_id,
            id_tag=self.id_tag,
            meter_stop=1500,
            timestamp=datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
            reason=reason  # Usando o valor correto
        )
        try:
            await self.call(request)
            self.transaction_id = None
            self.gui.log_message("✅ Transação encerrada com sucesso")
        except websockets.exceptions.ConnectionClosed:
            self.gui.log_message("❌ Conexão fechada durante parada de transação")
            self.connected = False
        except Exception as e:
            self.gui.log_message(f"❌ Erro ao parar transação: {str(e)}")


class EVChargerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Simulador de Carregador VE - OCPP 1.6")
        self.setGeometry(100, 100, 800, 600)
        
        # Variáveis de estado
        self.cp = None
        self.websocket = None
        self.charging = False
        self.energy_wh = 0
        self.power_w = 0
        
        # Layout principal
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        
        # Grupo de configuração
        config_group = QGroupBox("Configuração do Carregador")
        config_layout = QVBoxLayout()
        
        # ID do carregador
        id_layout = QHBoxLayout()
        id_layout.addWidget(QLabel("ID do Carregador:"))
        self.charger_id_input = QLineEdit("CP_SIMULATOR01")
        id_layout.addWidget(self.charger_id_input)
        config_layout.addLayout(id_layout)
        
        # URL do servidor
        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("URL do Servidor:"))
        self.server_url_input = QLineEdit("ws://172.18.3.132:9000/ws/")
        url_layout.addWidget(self.server_url_input)
        config_layout.addLayout(url_layout)
        
        # Botão de conexão
        self.connect_button = QPushButton("Conectar")
        self.connect_button.clicked.connect(self.toggle_connection)
        config_layout.addWidget(self.connect_button)
        
        config_group.setLayout(config_layout)
        main_layout.addWidget(config_group)
        
        # Grupo de status
        status_group = QGroupBox("Status do Carregador")
        status_layout = QVBoxLayout()
        
        # Status de conexão
        self.connection_status = QLabel("Desconectado")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        status_layout.addWidget(self.connection_status)
        
        # Status de carregamento
        self.charging_status = QLabel("Desconectado")
        self.charging_status.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.charging_status)
        
        # Informações de energia
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
        
        # Grupo de controle
        control_group = QGroupBox("Controles")
        control_layout = QHBoxLayout()
        
        # Botões de controle
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
        
        # Grupo de logs
        log_group = QGroupBox("Log de Mensagens")
        log_layout = QVBoxLayout()
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        log_layout.addWidget(self.log_display)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group, 1)
        
        # Timer para atualizar a interface
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.update_ui)
        self.ui_timer.start(500)
        
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

    def toggle_connection(self):
        if self.cp:
            self.disconnect_from_server()
        else:
            self.connect_to_server()

    async def async_connect_to_server(self):
        charger_id = self.charger_id_input.text()
        server_url = self.server_url_input.text() + charger_id
        
        while True:
            try:
                self.log_message(f"🌐 Tentando conectar ao servidor em {server_url}...")
                self.websocket = await websockets.connect(server_url, subprotocols=["ocpp1.6"])
                self.cp = CP_Simulator(charger_id, self.websocket, self)
                self.connection_status.setText("Conectado")
                self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                self.log_message("✅ Conectado com sucesso ao servidor OCPP!")
                self.update_charging_status("Disponível")

                # Inicia o ChargePoint
                await self.cp.start()
                break  # Sai do loop de reconexão

            except Exception as e:
                self.log_message(f"❌ Falha na conexão: {str(e)}")
                self.connection_status.setText("Erro de conexão")
                self.connection_status.setStyleSheet("color: red; font-weight: bold;")
                self.cp = None

                if self.websocket:
                    await self.websocket.close()

                await asyncio.sleep(5)  # espera 5s antes de tentar de novo

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

    def start_charging(self):
        if self.cp and not self.cp.charging:
            self.log_message("⚡ Iniciando carregamento localmente...")
            loop = asyncio.get_event_loop()
            loop.create_task(self.cp.start_charging_sequence(self.cp.id_tag))

    def stop_charging(self):
        if self.cp and self.cp.charging:
            self.log_message("🛑 Parando carregamento localmente...")
            self.cp.charging = False
            loop = asyncio.get_event_loop()
            # Usando "Local" para parada manual através da interface
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