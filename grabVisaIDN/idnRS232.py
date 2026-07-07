import serial

# The /dev path of your USB-to-serial adapter. Confirm with:
#   python -c "import serial.tools.list_ports as p; print([x.device for x in p.comports()])"
PORT = "/dev/cu.usbserial-AB82PGGV"

# MUST match the value set in the supply's front-panel System / Utility menu.
# SPD3303X-E supports 4800 / 9600 / 19200 / 38400 / 57600 / 115200.
BAUD = 9600

# Build the port object WITHOUT opening it yet, so a config/open failure is
# caught inside the try block instead of crashing at construction time.
ser = serial.Serial()
ser.port = PORT
ser.baudrate = BAUD
ser.bytesize = serial.EIGHTBITS
ser.parity = serial.PARITY_NONE
ser.stopbits = serial.STOPBITS_ONE
ser.timeout = 2  # seconds; plenty for RS-232

device_name = input("device name: ")

try:
    ser.open()

    # Drop any stale bytes sitting in the serial buffers.
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # SPD3303X-E uses newline framing only.
    ser.write(b"*IDN?\n")
    idn = ser.readline().decode(errors="replace").strip()

    if not idn:
        raise RuntimeError("No response to *IDN? - check baud rate and cabling.")

    print("IDN:", idn)

    with open("idn_output.txt", "a") as f:
        f.write(", ".join((device_name, idn)) + "\n")
    print("IDN written to idn_output.txt")

except serial.SerialException as e:
    print(f"\nSerial error: {e}")
    print("Checklist:")
    print("  1. Baud rate must match the supply's System / Utility menu.")
    print("  2. RS-232 may need a null-modem (crossover) cable - TX/RX swapped.")
    print("  3. Re-confirm the port path with the list_ports command above.")
finally:
    if ser.is_open:
        ser.close()
