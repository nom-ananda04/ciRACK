import pyvisa
import time

rm = pyvisa.ResourceManager()
resources = rm.list_resources()
usb_resources = [r for r in resources if r.startswith("USB")]
if not usb_resources:
    raise RuntimeError("No USB instrument found")

visa_resource_name = usb_resources[0]
print(f"Connecting to: {visa_resource_name}")
instrument = rm.open_resource(visa_resource_name)

# --- SPD3303X-E specifics ---
instrument.write_termination = '\n'   # SPD accepts \n ONLY
instrument.read_termination = '\n'
instrument.timeout = 5000              # 5 seconds is plenty

device_name = input("device name: ")

try:
    n = instrument.write('*IDN?')
    print("Bytes written:", n)
    time.sleep(0.3)
    
    raw_bytes, status = instrument.visalib.read(instrument.session, 64)
    print("RAW data inside buffer:", repr(raw_bytes))
    
    idn_string = raw_bytes.decode('utf-8', errors='ignore').strip()
    print("Decoded IDN string:", idn_string)
    
    with open('idn_output.txt', 'a') as f:   
        f.write(", ".join((device_name, idn_string)) + "\n")
    print("IDN successfully written to idn_output.txt")
    
except pyvisa.errors.VisaIOError as e:
    print(f"\nVISA Error: {e}")
    print("-> REMINDER: If this keeps timing out, you MUST physically turn the")
    print("   Siglent power supply switch OFF and ON again to unfreeze its USB port!")
finally:
    instrument.close()

