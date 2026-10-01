# VHS tape that records the AgentFare demo from a real terminal. This is the alternative to the committed GIF: the one in the
# README is drawn by scripts/make_assets.py from the same demo data, so it needs no terminal and no recorder. Record this one with:
#
#   . .venv/bin/activate     # so that `agentfare` is on PATH inside the recording shell
#   vhs demo.tape            # writes docs/demo.gif
#
# The tape only starts scripts/demo.sh, which runs the packaged demo data (DEMO_SET=demo) with a temporary data directory and a
# frozen clock, so the recording never touches your real logs or ~/.agentfare.
# Step by step instructions, size and theme suggestions and the asciinema alternative: docs/DEMO.md.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1100
Set Height 800
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set Framerate 24

# One screen per step: the script clears the screen before each command and waits 3 seconds after its output.
Env DEMO_SET "demo"
Env DEMO_CLEAR "1"
Env DEMO_PAUSE "3"
Env DEMO_TYPE "1"

Type "bash scripts/demo.sh"
Sleep 600ms
Enter

# The script ends with this sentence; the recording stops a moment later.
Wait@120s /own logs/
Sleep 3s
