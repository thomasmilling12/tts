TTS Discord Bot - Setup Instructions
======================================

FILES:
  main.py          - The bot code
  requirements.txt - Python dependencies
  .env.example     - Rename to .env and add your token

RASPBERRY PI SETUP:
  1. mkdir ~/ttsbot && cd ~/ttsbot
  2. python3 -m venv venv
  3. source venv/bin/activate
  4. pip install -r requirements.txt
  5. cp .env.example .env
  6. nano .env  (add your DISCORD_TOKEN)
  7. sudo apt install libopus0 ffmpeg
  8. python3 main.py  (test it runs)

SYSTEMD SERVICE (auto-start on boot):
  sudo nano /etc/systemd/system/ttsbot.service

  [Unit]
  Description=TTS Discord Bot
  After=network.target

  [Service]
  User=tmilling
  WorkingDirectory=/home/tmilling/ttsbot
  ExecStart=/home/tmilling/ttsbot/venv/bin/python3 main.py
  Restart=always

  [Install]
  WantedBy=multi-user.target

  Then run:
  sudo systemctl daemon-reload
  sudo systemctl enable ttsbot
  sudo systemctl start ttsbot

COMMANDS:
  /join              - Bot joins your voice channel
  /leave             - Bot leaves
  /skip              - Skip current TTS message
  /tts_on/off        - Enable/disable TTS
  /sayname_on/off    - Toggle username prefix
  /setlang           - Change TTS language (en, es, fr, etc)
  /setmaxlength      - Set max characters per message
  /ignore @user      - Ignore a user
  /unignore @user    - Un-ignore a user
  /setnomic          - Set no-mic text channel
  /samevc_on/off     - Require user to be in same VC
  /smartfilter_on/off- Filter links and emojis
  /tts_status        - Show current settings
  /panel             - Show all commands
