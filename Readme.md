# Drugs checker
This is an proof of concept project writen in [python](https://www.python.org/).
This project is meant to write a custom client for game named Mines formerly named Fodinae, located at 90.188.7.54:8090.

## Current features
1. It can connect to the game server, mentain connection and reconnect to the server on demand
2. It has basic TUI with shell like input which lets you edit commands with ease

## Current existing commands and keybindings

### Log scrolling
- **Page Up** — scroll log up one page
- **Page Down** — scroll log down one page
- **Home** — jump to oldest buffered line
- **End** — jump back to live tail

### Emacs line editing
- **Ctrl+A** / **Ctrl+E** — beginning / end of line
- **Ctrl+F** / **Ctrl+B** — forward / backward character
- **Alt+F** / **Alt+B** — forward / backward word
- **Ctrl+D** — delete character forward
- **Backspace** / **Ctrl+H** — delete character backward
- **Ctrl+T** — transpose characters
- **Ctrl+K** — kill to end of line
- **Ctrl+U** — kill whole line
- **Ctrl+W** — kill word backward
- **Alt+D** — kill word forward
- **Ctrl+Y** — yank (paste last kill)
- **Ctrl+P** / **Up** — previous command
- **Ctrl+N** / **Down** — next command
- **Ctrl+L** — redraw screen
## How to run the project
First of all you should have the robots.json in the same directory as the script.
The structure of the json file is like next.
```json
[
    {// this is an entry
        "name": "Robot name that would get displayed in the checker.",
        "hwid": "A random string that would be your hardware id.",
        "uniq": "This can be empty becouse the server sends it on connect",
        "hash": "You extract this from the regs of the game.",
        "id": "Your real in game id",
        "isLoggedIn": false
    },
    // next entry of another robot that you have
]
```

```bash
git clone https://github.com/Touch-the-rosess/DrugsChecker.git
cd DrugsChecker
python3 checker_standalone.py
```
