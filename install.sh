#!/data/data/com.termux/files/usr/bin/sh
# pdm installer (Termux)
set -e

DIR=$(cd "$(dirname "$0")" && pwd)
BIN="${PREFIX:-/data/data/com.termux/files/usr}/bin"

echo "== dependencies"
for tool in aria2c ffmpeg; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		case "$tool" in
			aria2c) pkg install -y aria2 ;;
			ffmpeg) pkg install -y ffmpeg ;;
		esac
	else
		echo "$tool already installed"
	fi
done

if ! command -v yt-dlp >/dev/null 2>&1; then
	command -v pip >/dev/null 2>&1 || pkg install -y python
	pip install -U yt-dlp
else
	echo "yt-dlp already installed"
fi

# YouTube's nsig challenge is solved by running the player JS.
if command -v node >/dev/null 2>&1 || command -v deno >/dev/null 2>&1; then
	echo "js runtime already installed"
else
	pkg install -y nodejs-lts
fi

# Progress notifications; the companion Termux:API app is installed separately.
if command -v termux-notification >/dev/null 2>&1; then
	echo "termux-api already installed"
else
	pkg install -y termux-api
	echo "notifications also need the Termux:API app (F-Droid)"
fi

echo "== storage access"
if [ ! -d "$HOME/storage" ] && [ ! -d /storage/emulated/0/Download ]; then
	echo "to save downloads to phone storage run: termux-setup-storage"
fi

echo "== pdm link"
chmod +x "$DIR/pdm.py"
ln -sf "$DIR/pdm.py" "$BIN/pdm"
echo "$BIN/pdm -> $DIR/pdm.py"

echo
"$BIN/pdm" doctor
echo
echo "usage: pdm get <link>"
