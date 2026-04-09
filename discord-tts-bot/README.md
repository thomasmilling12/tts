# Discord TTS Bot

This project is a Discord bot that implements Text-to-Speech (TTS) functionality, allowing users to have messages read aloud in a voice channel. The bot is designed to enhance communication in Discord servers by providing an audio representation of text messages.

## Features

- Automatically joins a voice channel when a user enters.
- Reads messages from a designated text channel aloud.
- Supports multiple TTS settings, including language selection and message length limits.
- Allows users to ignore specific members' messages.
- Provides commands for managing TTS settings and bot behavior.

## Setup Instructions

1. **Clone the Repository**
   ```bash
   git clone <repository-url>
   cd discord-tts-bot
   ```

2. **Install Dependencies**
   Make sure you have Python 3.8 or higher installed. Then, install the required packages using pip:
   ```bash
   pip install -r requirements.txt
   ```

3. **Create a Discord Bot**
   - Go to the [Discord Developer Portal](https://discord.com/developers/applications).
   - Create a new application and add a bot to it.
   - Copy the bot token for later use.

4. **Configure the Bot**
   - Create a file named `config.json` in the root directory with the following structure:
     ```json
     {
       "token": "YOUR_BOT_TOKEN"
     }
     ```
   - Replace `YOUR_BOT_TOKEN` with the token you copied from the Discord Developer Portal.

5. **Run the Bot**
   Execute the following command to start the bot:
   ```bash
   python bot.py
   ```

## Usage

- Join a voice channel in your Discord server.
- Use the `/setnomic` command to specify which text channel the bot should read messages from.
- The bot will automatically read messages aloud as they are sent in the designated channel.

## Commands

- `/join`: Join your voice channel.
- `/leave`: Leave the current voice channel.
- `/skip`: Stop the current message being read.
- `/ignore @user`: Stop reading a specific user's messages.
- `/unignore @user`: Resume reading a specific user's messages.
- `/tts_status`: Show current TTS settings.

## Contributing

Contributions are welcome! Please feel free to submit a pull request or open an issue for any enhancements or bug fixes.

## License

This project is licensed under the MIT License. See the LICENSE file for more details.