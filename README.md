# Bot bán hàng tự động trên Telegram

Bot Telegram bán sản phẩm số (tài khoản, key bản quyền, mã thẻ, link tải…):
khách chọn sản phẩm → nhận mã QR chuyển khoản (VietQR) → thanh toán → **bot tự gửi hàng**.

## Tính năng

- Menu sản phẩm dạng nút bấm, hiển thị giá và tồn kho
- Tạo đơn với mã riêng (VD `DH7K2P9Q`), kèm **mã QR VietQR** đã điền sẵn số tiền và nội dung
- Giữ hàng cho đơn đang chờ, **tự huỷ đơn** quá hạn (mặc định 30 phút)
- **Tự động giao hàng** khi thanh toán được xác nhận:
  - tự động qua webhook ngân hàng (SePay / Casso), hoặc
  - admin bấm nút "✅ Đã nhận tiền" / gõ `/confirm MÃ_ĐƠN`
- Khách xem lại lịch sử đơn và hàng đã mua
- Lệnh quản trị: thêm sản phẩm, nạp kho, đổi giá, ẩn/hiện, xem đơn, thống kê doanh thu
- Lưu trữ bằng SQLite, không cần cài database

## Hướng dẫn từng bước

### 1. Tạo bot với BotFather

1. Mở Telegram, tìm **@BotFather** → gõ `/newbot`
2. Đặt tên hiển thị và username (phải kết thúc bằng `bot`, VD `shopcuatoi_bot`)
3. BotFather trả về **token** dạng `123456789:ABC...` — giữ bí mật token này
4. Lấy **ID Telegram** của bạn (để làm admin) bằng cách nhắn cho **@userinfobot**

### 2. Cài đặt

Yêu cầu Python 3.10+.

```bash
git clone <repo> && cd luutrumat
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # rồi mở .env và điền BOT_TOKEN, ADMIN_IDS, thông tin ngân hàng
```

`BANK_CODE` là mã ngân hàng theo VietQR, ví dụ `MB`, `VCB`, `TCB`, `ACB`, `BIDV`, `VPB`, `TPB`…

### 3. Chạy bot

```bash
python -m salesbot.bot
```

### 4. Thêm sản phẩm và nạp hàng

Nhắn cho bot (từ tài khoản admin):

```
/addproduct Netflix Premium 1 tháng | 65000 | Tài khoản dùng riêng, bảo hành 30 ngày
```

Bot trả về ID sản phẩm (VD `#1`). Nạp hàng — mỗi dòng là một hàng sẽ được giao cho khách:

```
/addstock 1
user1@mail.com | matkhau1
user2@mail.com | matkhau2
```

Gõ `/admin` để xem toàn bộ lệnh quản trị.

### 5. Khách mua hàng

Khách gõ `/start` → **🛍 Sản phẩm** → chọn sản phẩm → **Mua 1** → bot gửi mã QR.
Khi thanh toán được xác nhận, bot gửi ngay nội dung hàng cho khách.

## Thông báo cho khách

Bot ghi nhớ mọi người đã nhắn tin với nó, để gửi thông báo:

- **Có hàng lại (tự động):** khi `/addstock` cho sản phẩm đang hết hàng, bot gửi
  "🔥 TIN NÓNG — … đã có hàng lại!" kèm số lượng, giá và nút **🛒 Mua ngay** cho mọi khách.
  Tắt bằng `AUTO_RESTOCK_NOTIFY=false`. Gửi lại bất cứ lúc nào bằng `/notify ID`.
- **Tin khuyến mãi:** gửi cho bot một tin (chữ, hoặc ảnh kèm chú thích), rồi **Trả lời** tin đó
  bằng `/broadcast` — bot gửi y nguyên cho mọi khách. `/broadcast 3` để kèm nút Mua ngay sản phẩm #3.
- **Emoji động (như nhãn "TIN NÓNG" nhấp nháy):** `/setemoji badge <emoji>`, `/setemoji stock <emoji>`,
  `/setemoji price <emoji>`. Cần tài khoản chủ bot có Telegram Premium; nếu không, bot tự dùng emoji thường.
- **Kênh Telegram (tuỳ chọn):** đặt `CHANNEL_ID=@kenhcuashop` và thêm bot làm admin kênh;
  thông báo có hàng sẽ được đăng lên kênh, nút Mua ngay mở bot đúng sản phẩm.

## Nhóm sản phẩm có logo

Trang quản trị → **Danh mục**: tạo nhóm (tên, emoji thường, mã logo, thứ tự) rồi gán sản phẩm vào nhóm.
Khách bấm 🛍 Sản phẩm sẽ thấy các nhóm 2 nút mỗi hàng kèm số hàng còn. Logo trên nút là emoji động
(`icon_custom_emoji_id`): lấy mã bằng lệnh `/emojiid` trong bot; chỉ hiện khi chủ bot có Telegram Premium,
nếu không bot tự dùng emoji thường.

## Trang quản trị web

Đặt `ADMIN_PASSWORD=...` (ít nhất 8 ký tự) trong `.env`, khởi động lại bot rồi mở
`http://localhost:8080/admin` (gõ `/web` trong bot để xem các địa chỉ). Gồm:

- **Tổng quan:** doanh thu hôm nay / 7 ngày / tháng / tổng, biểu đồ theo ngày, sản phẩm bán chạy, sắp hết hàng
- **Sản phẩm & kho:** thêm, sửa, ẩn/hiện; nhập kho từ Excel (.xlsx nhiều cột được ghép `a | b | c`), CSV, TXT
  hoặc dán trực tiếp; bỏ qua hàng trùng; xoá hàng chưa bán
- **Đơn hàng:** lọc theo trạng thái/ngày/khách, xác nhận đã nhận tiền, huỷ, gửi lại hàng, **xuất Excel**
- **Thông báo:** soạn tin khuyến mãi có ảnh + nút Mua ngay, gửi thử cho mình, gửi ngay hoặc **hẹn giờ**;
  bật/tắt thông báo "có hàng lại" tự động

Truy cập từ xa: cài [Tailscale](https://tailscale.com) trên máy chạy bot và trên điện thoại, rồi mở
`http://<tên-máy>:8080/admin`.

## Sao lưu & khôi phục

- Bot tự tạo bản sao lưu **mỗi giờ** vào `BACKUP_DIR` (nên đặt trong iCloud Drive) và **gửi qua Telegram cho admin
  mỗi ngày** lúc `BACKUP_TELEGRAM_HOUR` giờ. Mỗi bản là 1 file zip gồm `shop.db`, `.env` và thư mục `uploads/`.
- Sao lưu ngay: lệnh `/backup` trong bot, hoặc nút **💾 Sao lưu** trên trang quản trị.
- Khôi phục trên máy Mac mới: cài Python, tải code về `~/salesbot`, rồi chạy
  `bash ~/salesbot/setup_mac.sh ~/Downloads/salesbot-backup-....zip` — script cài thư viện, khôi phục dữ liệu
  và cho bot chạy ngầm 24/24.

## Tự động xác nhận thanh toán (tuỳ chọn)

Mặc định admin nhận thông báo mỗi đơn mới và bấm **✅ Đã nhận tiền** để giao hàng.
Để hoàn toàn tự động, dùng dịch vụ theo dõi biến động số dư như **SePay** hoặc **Casso**:

1. Trong `.env`: `WEBHOOK_ENABLED=true`, đặt `WEBHOOK_SECRET` là một chuỗi ngẫu nhiên
2. Chạy bot trên máy có địa chỉ công khai (VPS) hoặc dùng `ngrok http 8080` khi thử nghiệm
3. Trong SePay/Casso, thêm webhook trỏ tới `https://<domain>/payment`
   - SePay: kiểu xác thực **API Key**, nhập đúng `WEBHOOK_SECRET`
   - Casso: điền `WEBHOOK_SECRET` vào ô **Secure Token**
   - Hoặc thêm `?secret=<WEBHOOK_SECRET>` vào cuối URL

Bot tìm mã đơn trong nội dung chuyển khoản; nếu số tiền đủ, đơn được đánh dấu đã thanh toán và hàng được gửi ngay.

## Chạy 24/7 trên VPS (systemd)

```ini
# /etc/systemd/system/salesbot.service
[Unit]
Description=Telegram sales bot
After=network.target

[Service]
WorkingDirectory=/opt/luutrumat
ExecStart=/opt/luutrumat/.venv/bin/python -m salesbot.bot
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now salesbot
```

Nhớ sao lưu file `shop.db` định kỳ — đây là nơi lưu sản phẩm, kho hàng và đơn hàng.

## Cấu trúc mã nguồn

| File | Nội dung |
| --- | --- |
| `salesbot/config.py` | Đọc cấu hình từ `.env` |
| `salesbot/db.py` | Lưu trữ SQLite: sản phẩm, kho, đơn hàng |
| `salesbot/bot.py` | Giao diện khách, lệnh admin, giao hàng, webhook thanh toán |
