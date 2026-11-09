import pyautogui
import time
import os

print("Script start ho gayi hai. Fauran apni ERP wali screen samne open kar lein (5 seconds ka time hai)...")
time.sleep(5)

test_complaint = "282636629" # Dummy test number
test_folder = os.path.abspath("Test_Output")
if not os.path.exists(test_folder):
    os.makedirs(test_folder)

print("1. Complaint number enter kar raha hai (Double click + delete)...")
pyautogui.click(x=118, y=160)
time.sleep(0.5)
pyautogui.doubleClick(x=118, y=160)
time.sleep(0.5)
pyautogui.press('backspace')
pyautogui.write(test_complaint)
pyautogui.press('enter')

# Record load hone ka wait
time.sleep(3) 

print("2. Main Print button click kar raha hai...")
pyautogui.click(x=1196, y=363)
time.sleep(2) # Popup aane ka wait

print("3. FOR HA FIELD Checkbox click kar raha hai...")
pyautogui.click(x=764, y=419)
time.sleep(1)

print("4. Popup Print button click kar raha hai...")
pyautogui.click(x=671, y=493)

print("5. Browser khulne aur PDF load hone ka wait (8 seconds)...")
time.sleep(8) 

print("6. Foxit Print command (Ctrl+P)...")
pyautogui.hotkey('ctrl', 'p')
time.sleep(2.5) # Wait for Foxit Print module
pyautogui.press('enter') # First enter: Foxit OK
time.sleep(3) # Wait for Save As dialog

# Save dialog mein file path write karega
file_path = os.path.join(test_folder, f"{test_complaint}.pdf")
pyautogui.write(file_path)
time.sleep(1)
pyautogui.press('enter') # Second enter: Save
time.sleep(4)

print("7. ERP window par switch kar raha hai (Alt+Tab)...")
pyautogui.hotkey('alt', 'tab')
time.sleep(2)

print("8. Print module popup close kar raha hai (Exit button)...")
pyautogui.click(x=749, y=493)
time.sleep(1)

print("✅ Single record test run complete!")
