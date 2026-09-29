#!/bin/bash
# Cài đặt hoặc khôi phục bot trên máy Mac, rồi cho bot tự chạy ngầm 24/24.
#
#   bash ~/salesbot/setup_mac.sh                          # cài mới (dùng .env đang có)
#   bash ~/salesbot/setup_mac.sh ~/Downloads/salesbot-backup-....zip   # khôi phục từ bản sao lưu
#
# Yêu cầu: đã cài Python 3.10+ từ python.org (và chạy Install Certificates.command).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
LABEL="com.$(whoami).salesbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "📁 Thư mục bot: $DIR"

# 1. Tìm Python 3.10 trở lên
PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "❌ Chưa có Python 3.10 trở lên. Cài từ https://www.python.org/downloads/macos/ rồi chạy lại."
    exit 1
fi
echo "🐍 Dùng $($PY --version)"

# 2. Dừng bot cũ trên máy này (nếu có) để không chạy 2 bot cùng lúc
launchctl unload "$PLIST" 2>/dev/null || true

# 3. Khôi phục bản sao lưu (nếu có)
if [ $# -ge 1 ]; then
    BACKUP="$1"
    if [ ! -f "$BACKUP" ]; then
        echo "❌ Không tìm thấy file sao lưu: $BACKUP"
        exit 1
    fi
    if [ -f shop.db ]; then
        OLD="shop.db.truoc-khoi-phuc-$(date +%Y%m%d-%H%M%S)"
        mv shop.db "$OLD"
        echo "📦 Đã cất dữ liệu cũ thành $OLD"
    fi
    unzip -o -q "$BACKUP" -d "$DIR"
    echo "✅ Đã khôi phục dữ liệu từ $(basename "$BACKUP")"
fi

# 4. File cấu hình
if [ ! -f .env ]; then
    cp .env.example .env
    echo "⚠️  Chưa có file .env. Đã tạo từ mẫu và mở ra — điền BOT_TOKEN, ADMIN_IDS... rồi chạy lại lệnh này."
    open -e .env
    exit 1
fi

# 5. Thư viện
echo "📚 Đang cài thư viện (khoảng 1 phút)…"
"$PY" -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

# 6. Chạy ngầm và tự khởi động cùng máy
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-i</string>
        <string>$DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>salesbot.bot</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$DIR/bot.log</string>
    <key>StandardErrorPath</key><string>$DIR/bot.log</string>
</dict>
</plist>
PLIST
launchctl load -w "$PLIST"

sleep 5
if launchctl list | grep -q "$LABEL"; then
    echo ""
    echo "🎉 Xong! Bot đang chạy ngầm và sẽ tự bật lại khi khởi động máy."
    echo "   • Nhắn /start cho bot trên Telegram để kiểm tra"
    echo "   • Trang quản trị: http://localhost:8080/admin"
    echo "   • Xem log:        tail -f \"$DIR/bot.log\""
    echo "   • Khởi động lại:  launchctl kickstart -k gui/\$(id -u)/$LABEL"
else
    echo "❌ Bot chưa chạy được. Xem lỗi: tail -n 30 \"$DIR/bot.log\""
    exit 1
fi
