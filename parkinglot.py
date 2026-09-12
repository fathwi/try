import time
import os
import csv
from datetime import datetime
import paho.mqtt.client as mqtt
from gpiozero import DistanceSensor, LineSensor, LED, Servo
from rpi_lcd import LCD
import board
import busio
from adafruit_pn532.i2c import PN532_I2C

# --- MQTT Configuration ---
MQTT_BROKER = "b7c2435d3b9f4c30911ab76c046191a2.s1.eu.hivemq.cloud" 
MQTT_PORT = 8883
MQTT_USERNAME = "hivemq.webclient.1789212560818"
MQTT_PASSWORD = "R5XDOC*okP%yro3b6C5mJ3bU90$GCM4e"

MQTT_TOPIC_IR1 = "omar/ir1"
MQTT_TOPIC_IR2 = "omar/ir2"
MQTT_TOPIC_DURATION = "omar/duration"

# --- Hardware Configuration (BCM Pin Numbers) ---
TRIG_PIN = 27
ECHO_PIN = 22
IR_SLOT1_PIN = 4
IR_SLOT2_PIN = 5
SERVO_PIN = 13
LED_RED_PIN = 10
LED_YELLOW_PIN = 11
LED_GREEN_PIN = 9

# --- Log File Configuration ---
GATE_NO = "3"  
LOG_DIR = os.path.expanduser("~/gate_logs")
LOG_FILE = f"{LOG_DIR}/gate_{GATE_NO}_log.csv"
QUEUE_FILE = f"{LOG_DIR}/waiting_queue.csv"

os.makedirs(LOG_DIR, exist_ok=True)

# --- Initialize I2C and LCD ---
try:
    # Try default address 0x27 first
    lcd = LCD(address=0x27)
except Exception:
    try:
        # Fallback to 0x3f (common for many 16x2 I2C modules)
        lcd = LCD(address=0x3f)
    except Exception as e:
        print(f"LCD completely failed to initialize: {e}")
        lcd = None

# --- Initialize NFC (PN532) ---
try:
    i2c_bus = busio.I2C(board.SCL, board.SDA)
    pn532 = PN532_I2C(i2c_bus, debug=False)
    pn532.SAM_configuration()
    print("NFC Reader Initialized.")
except Exception as e:
    print(f"NFC Reader failed to initialize: {e}")
    pn532 = None

# --- Initialize Sensors & Actuators ---
ultrasonic = DistanceSensor(echo=ECHO_PIN, trigger=TRIG_PIN)
ir_slot1 = LineSensor(IR_SLOT1_PIN)
ir_slot2 = LineSensor(IR_SLOT2_PIN)
servo = Servo(SERVO_PIN)

led_red = LED(LED_RED_PIN)
led_yellow = LED(LED_YELLOW_PIN)
led_green = LED(LED_GREEN_PIN)

# --- MQTT Setup ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Successfully connected to MQTT Broker!")

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
mqtt_client.tls_set()
mqtt_client.on_connect = on_connect

mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start() 

# --- Helper Functions ---
def log_event(event_type, car_id, slot="-", duration="-", cost="-", notes="-"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Event Type", "Car ID", "Gate", "Slot", "Duration", "Cost", "Notes"])
        writer.writerow([timestamp, event_type, car_id, GATE_NO, slot, duration, cost, notes])

def add_to_queue(car_id):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(QUEUE_FILE, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, car_id])

def get_distance_zone(sensor):
    dist_cm = sensor.distance * 100
    if dist_cm > 30:
        return "A"  
    elif 10 <= dist_cm <= 30:
        return "B"  
    else:
        return "C"  

def read_real_nfc():
    """Reads the actual PN532 hardware."""
    if not pn532:
        time.sleep(2)
        return "NFC-ERROR"
    
    print("Waiting for NFC tap...")
    while True:
        # Timeout prevents the script from freezing forever
        uid = pn532.read_passive_target(timeout=0.5)
        if uid is not None:
            car_id = uid.hex().upper()
            print(f"Card Detected! ID: {car_id}")
            # Beep or pause to prevent double-reading immediately
            time.sleep(1)
            return car_id

def open_gate():
    servo.max()
    time.sleep(1) # Give mechanical servo time to move

def close_gate():
    servo.min()
    time.sleep(1) # Give mechanical servo time to move

# --- Main Entry/Exit Logic Loop ---
print(f"Gate {GATE_NO} System Controller Running...")
if lcd:
    lcd.clear()
    lcd.text("System Online", 1)

# Ensure gate starts closed
close_gate()

previous_zone = "A"
active_entries = {}  
prev_slot1_state = None
prev_slot2_state = None

try:
    while True:
        current_zone = get_distance_zone(ultrasonic)
        
        slot1_occupied = not ir_slot1.value
        slot2_occupied = not ir_slot2.value
        total_occupied = sum([slot1_occupied, slot2_occupied])
        
        # --- 1. MQTT Publish: IR Sensor 1 ---
        if slot1_occupied != prev_slot1_state:
            state_msg = "FULL" if slot1_occupied else "EMPTY"
            mqtt_client.publish(MQTT_TOPIC_IR1, state_msg)
            prev_slot1_state = slot1_occupied

        # --- 2. MQTT Publish: IR Sensor 2 ---
        if slot2_occupied != prev_slot2_state:
            state_msg = "FULL" if slot2_occupied else "EMPTY"
            mqtt_client.publish(MQTT_TOPIC_IR2, state_msg)
            prev_slot2_state = slot2_occupied

        # --- VEHICLE ATTEMPTING ENTRY (Zone A -> Zone B) ---
        if previous_zone == "A" and current_zone == "B":
            if lcd:
                lcd.clear()
                lcd.text("Please tap your", 1)
                lcd.text("NFC card", 2)
            
            # Use the real hardware NFC reading
            car_id = read_real_nfc()
            
            if total_occupied < 2:
                assigned_slot = "1" if not slot1_occupied else "2"
                active_entries[car_id] = datetime.now()
                
                if lcd:
                    lcd.clear()
                    lcd.text(f"Parked in Slot {assigned_slot}", 1)
                    lcd.text("- Welcome", 2)
                
                open_gate()  
                log_event("ENTRY", car_id, slot=assigned_slot, notes=f"Assigned slot {assigned_slot}")
            else:
                if lcd:
                    lcd.clear()
                    lcd.text("GARAGE FULL", 1)
                    lcd.text("- Please wait", 2)
                
                add_to_queue(car_id)
                log_event("WAITING", car_id, notes="Garage full — queued")
                time.sleep(3)
        
        # --- VEHICLE COMPLETES DIRECTION TRANSITION (Zone B -> Zone C) ---
        elif previous_zone == "B" and current_zone == "C":
            # Vehicle passed safely inside
            time.sleep(2)
            close_gate()  

        # --- VEHICLE ATTEMPTING EXIT (Zone C -> Zone B) ---
        elif previous_zone == "C" and current_zone == "B":
            if lcd:
                lcd.clear()
                lcd.text("Exiting... Tap", 1)
                lcd.text("NFC Card", 2)
                
            car_id = read_real_nfc()
            
            cost_str = "0.00"
            if car_id in active_entries:
                entry_time = active_entries.pop(car_id)
                duration_delta = datetime.now() - entry_time
                total_seconds = duration_delta.total_seconds()
                
                cost_value = (total_seconds / 30.0) * 2.0
                cost_str = f"${cost_value:.2f}"
                
                minutes, seconds = divmod(int(total_seconds), 60)
                duration_str = f"{minutes}m {seconds}s"
                lcd_duration = f"{minutes // 60}h {minutes % 60}m"
                
                print("\n" + "="*35)
                print("         EXIT RECEIPT")
                print("="*35)
                print(f"NFC ID       : {car_id}")
                print(f"Time Stayed  : {duration_str} ({int(total_seconds)}s)")
                print(f"Total Cost   : {cost_str}")
                print("="*35 + "\n")
                
                duration_payload = f"Car: {car_id} | Time: {duration_str} | Cost: {cost_str}"
                mqtt_client.publish(MQTT_TOPIC_DURATION, duration_payload)
                
                if lcd:
                    lcd.clear()
                    lcd.text("Thank you", 1)
                    lcd.text(f"Duration: {lcd_duration}", 2)

            else:
                duration_str = "Unknown"
                if lcd:
                    lcd.clear()
                    lcd.text("Error: Unregistered", 1)
                print(f"\n[WARNING] Unregistered exit for NFC: {car_id}\n")

            open_gate()  
            log_event("EXIT", car_id, duration=duration_str, cost=cost_str, notes="Slot freed")
            
        # --- VEHICLE COMPLETES EXIT TRANSITION (Zone B -> Zone A) ---
        elif previous_zone == "B" and current_zone == "A":
            time.sleep(2)
            close_gate() 
            if lcd:
                lcd.clear()
                lcd.text("Gate Secure", 1)

        # --- Dashboard Status Feedback (LEDs) ---
        if total_occupied == 2:
            led_red.on()
            led_yellow.off()
            led_green.off()
        elif total_occupied == 1:
            led_red.off()
            led_yellow.on()
            led_green.off()
        else:
            led_red.off()
            led_yellow.off()
            led_green.on()

        previous_zone = current_zone
        time.sleep(0.2)

except KeyboardInterrupt:
    print("\nShutting down gate operation software.")
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    close_gate()
    if lcd:
        lcd.clear()