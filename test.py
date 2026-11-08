import pyautogui
import time

print("Script start ho gayi hai. Fauran apni ERP wali screen samne open kar lein (5 seconds ka time hai)...")
time.sleep(5)

test_complaint = "282636629" # Yeh ek dummy test number hai

print("1. Complaint number enter kar raha hai...")
pyautogui.click(x=118, y=160)
pyautogui.hotkey('ctrl', 'a')
pyautogui.press('backspace')
pyautogui.write(test_complaint)
pyautogui.press('enter')

# Record load hone ka wait (Agar aapka internet/system slow hai tow is 3 ko 5 kar sakte hain)
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

print("6. Print command (Ctrl+P) bhej raha hai...")
pyautogui.hotkey('ctrl', 'p')
time.sleep(3) # Print window load hone ka wait
pyautogui.press('enter')
time.sleep(2) # Print command process hone ka wait

print("7. Browser tab close kar raha hai (Ctrl+W)...")
pyautogui.hotkey('ctrl', 'w')

print("Test run complete!")