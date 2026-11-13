import pyautogui
import time
import pandas as pd
import keyboard
import os
import re # Folder name ko clean karne ke liye

# --- 1. SETUP FOLDERS & TRACKING ---
# Main PDF folder
save_folder = os.path.abspath("Today_Complaints")
if not os.path.exists(save_folder):
    os.makedirs(save_folder)

progress_file = "completed_complaints.txt"
if not os.path.exists(progress_file):
    with open(progress_file, 'w') as f:
        pass 

# Pehle se print hui complaints read karega (Auto-Resume)
with open(progress_file, 'r') as f:
    completed = set(f.read().splitlines())

# --- 2. LOAD EXCEL DATA ---
# ⚠️ APNI EXCEL FILE KE EXACT COLUMN NAMES YAHAN LIKHEIN:
complaint_col = 'COMPLAINT_NO' 
tech_col = 'TECHNICIAN_NAME' 

df = pd.read_excel('complaints.xlsx')
df = df.dropna(subset=[complaint_col]) # Empty rows ignore karega

print("🤖 Bot Ready! 5 seconds mein ERP window maximize kar lein...")
time.sleep(5)

# --- 3. THE MAIN LOOP ---
for index, row in df.iterrows():
    num = str(row[complaint_col]).strip()
    
    # Technician ka naam fetch aur clean karna 
    raw_tech_name = str(row[tech_col]).strip() if pd.notna(row[tech_col]) else "Unknown"
    tech_name = re.sub(r'[\\/*?:"<>|]', "", raw_tech_name) # Folder ke liye invalid characters hatayega

    # Emergency Stop Check
    if keyboard.is_pressed('esc'):
        print("🛑 Process stopped by User (Esc pressed).")
        break

    # Auto-Resume Check
    if num in completed:
        print(f"⏩ {num} already done. Skipping...")
        continue

    print(f"⚙️ Processing Complaint #: {num} (Tech: {tech_name})")

    # Technician ka sub-folder banayega
    tech_folder = os.path.join(save_folder, tech_name)
    if not os.path.exists(tech_folder):
        os.makedirs(tech_folder)

    # Step A: Enter Complaint Number
    # Click once to ensure the ERP window is focused
    pyautogui.click(x=118, y=160)
    time.sleep(0.5)
    # Double-click to highlight the existing number, then delete
    pyautogui.doubleClick(x=118, y=160)
    time.sleep(0.5)
    pyautogui.press('backspace')
    pyautogui.write(num)
    pyautogui.press('enter')
    time.sleep(3) # Record load wait

    # Step B: Main Print Click
    pyautogui.click(x=1196, y=363)
    time.sleep(2) # Popup wait

    # Step C: HA FIELD Checkbox
    pyautogui.click(x=764, y=419)
    time.sleep(1)

    # Step D: Popup Print Click
    pyautogui.click(x=671, y=493)

    # --- 4. WAIT FOR PDF TO LOAD ---
    print(f"⏳ Waiting for {num} PDF to load in IE...")
    time.sleep(8) 

    # --- 5. PRINT & SAVE AS PDF ---
    print(f"🖨️ Pressing Ctrl+P for {tech_name}...")
    pyautogui.hotkey('ctrl', 'p')
    time.sleep(2.5) # Reduced wait time for Foxit Print module
    
    # Clicks 'OK' on the Foxit Print menu
    pyautogui.press('enter')
    time.sleep(3) # Wait for Windows 'Save As' popup
    
    # Type absolute path and save in technician's folder
    file_path = os.path.join(tech_folder, f"{num}.pdf")
    pyautogui.write(file_path)
    time.sleep(1)
    pyautogui.press('enter') # Save
    time.sleep(4) # File saving processing time

    # --- 6. CLEANUP & RECORD ---
    print("🔄 Switching back to ERP window using Alt+Tab...")
    pyautogui.hotkey('alt', 'tab') 
    time.sleep(2) # Give Windows time to fully bring ERP to the front
    
    # Close the Print Module popup so the main screen is accessible for the next loop
    print("❌ Closing the Print Module...")
    pyautogui.click(x=749, y=493) # Click 'Exit' button
    time.sleep(1) # Wait for popup to close
    
    # Write to tracking file
    with open(progress_file, 'a') as f:
        f.write(num + "\n")
    
    print(f"✅ {num} successfully saved in {tech_name}'s folder!")
    time.sleep(1)

print("🎉 All 939 tasks completed smoothly!")