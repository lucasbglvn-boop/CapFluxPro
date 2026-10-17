# TOOL — Quay livestream Bigo & TikTok

Công cụ tự host, quay livestream Bigo và TikTok chỉ bằng **ID phòng**.

Trước đây bạn phải mở phòng trên trình duyệt → F12 → tab Network → lọc `m3u8` → copy link thủ công. **TOOL làm việc đó tự động**: bạn chỉ cần nhập ID phòng (hoặc dán link), phần còn lại engine lo hết.

---

## 1. Cài đặt

### Yêu cầu

| Thứ cần | Ghi chú |
|---|---|
| **Python 3.10+** | Khi cài nhớ tick **"Add Python to PATH"** — [tải tại đây](https://www.python.org/downloads/) |
| **FFmpeg** | Bắt buộc, để ghi video Bigo — [tải tại đây](https://www.gyan.dev/ffmpeg/builds/) |

Với FFmpeg: giải nén ra một thư mục cố định (VD `C:\ffmpeg`), rồi thêm `C:\ffmpeg\bin` vào **PATH** của Windows (Start → "Edit the system environment variables" → Environment Variables → Path → New).

Kiểm tra đã đúng chưa bằng cách mở CMD và gõ:

```
python --version
ffmpeg -version
```

Cả hai phải chạy được.

### Chạy lần đầu

**Click đúp `start.bat`.** Script sẽ tự:

1. Tạo môi trường Python riêng (`venv/`)
2. Cài các thư viện cần thiết
3. Kiểm tra FFmpeg
4. Mở trình duyệt tới `http://127.0.0.1:5000`

Lần đầu chạy sẽ mất 1–2 phút để cài thư viện. Các lần sau mở rất nhanh.

---

## 2. Cách dùng

### Thêm phòng

1. Bấm **Thêm phòng**
2. Nhập **ID phòng** hoặc link vào ô đầu tiên
3. Chọn nền tảng (*Tự động (Bigo)* là mặc định) → bấm **Thêm**

TOOL nhận **rất nhiều dạng**, bạn không cần đoán định dạng đúng:

| Bạn nhập | TOOL hiểu là |
|---|---|
| `123456789` | ID số |
| `093725729` | ID số có số 0 đầu |
| `d325323` | ID chữ + số |
| `anubdn2s` | ID chữ |
| `numwarn12345678` | ID chữ + số dài |
| `it.352344` | ID có dấu chấm |
| `some.user` | ID có dấu chấm |
| `_ptthuytien` | ID **bắt đầu bằng `_`** |
| `.abc`, `-abc` | ID bắt đầu bằng `.` hoặc `-` |
| `nguyễn_văn_a`, `用户123` | ID có ký tự Unicode / có dấu |
| `@someuser` | Handle TikTok — **tự chuyển nền tảng sang TikTok** |
| `bigo.tv/abc` | Link thiếu `https://` |
| `www.bigo.tv/abc` | Link thiếu `https://` |
| `bigolive.tv/abc`, `bigoapp.tv/abc` | Tên miền Bigo khác → tự đổi về `bigo.tv` |
| `https://www.bigo.tv/abc` | Link đầy đủ |
| `https://www.bigo.tv/vi/123456789` | Link có mã ngôn ngữ |
| `https://m.bigo.tv/abc` | Link bản mobile |
| `https://www.tiktok.com/@abc/live` | Link TikTok |

Khoảng trắng thừa ở đầu/cuối được tự động cắt bỏ.

**Chỉ bị từ chối khi** ID chứa dấu cách hoặc một trong các ký tự `/ \ ? # % & = +`.
Nếu ID của bạn có ký tự đó, hãy dán link đầy đủ.

> **TikTok**: nhập `@username` (hoặc `username`) rồi chọn nền tảng **TikTok**.
> Để *Tự động* cũng được — TOOL thấy dấu `@` sẽ tự hiểu là TikTok.

> **Thêm phòng trùng**: TOOL chặn trùng theo URL đã chuẩn hóa, nên `_ptthuytien`,
> `bigolive.tv/_ptthuytien` và `https://www.bigo.tv/_ptthuytien` được coi là **cùng một phòng**
> — thêm lần thứ hai sẽ báo *"Phòng này đã có trong danh sách rồi."* Đây là chủ ý, để
> không quay trùng một phòng hai lần.

### Bảng phòng — 2 làn riêng biệt

Danh sách phòng **không trộn lẫn** các nền tảng. Mỗi nền tảng có làn riêng:

| Làn | Vị trí |
|---|---|
| **TikTok** ♪ | bên **trái** |
| **Bigo** 🎤 | bên **phải** |
| **Nền tảng khác** 📺 | làn thứ ba, **chỉ hiện khi** bạn thật sự có phòng thuộc nền tảng khác |

Mỗi làn có tiêu đề riêng kèm **số phòng** — số này **chuyển đỏ** khi trong làn có phòng đang live
hoặc đang quay. Ô tìm kiếm và sắp xếp áp dụng cho **cả hai làn cùng lúc**.

Trên màn hình hẹp (dưới 900px) các làn **xếp dọc** thay vì cạnh nhau, mỗi làn vẫn đủ chiều rộng.

> Nếu bạn chỉ dùng Bigo và TikTok thì luôn thấy đúng 2 làn: TikTok trái, Bigo phải.

### Theo dõi & quay

**Mỗi phòng bạn tự bật theo dõi.** Không có gì tự chạy khi bạn mở web.

1. Mở web → **tất cả phòng đã lưu hiện ra bình thường** (tên, avatar, ID).
   Không phòng nào bị liên hệ, không tốn tài nguyên, không quay gì.
2. Bấm **▶ Bắt đầu theo dõi** trên phòng bạn muốn → TOOL kiểm tra ngay lập tức:
   - **Đang live** → bắt đầu quay luôn.
   - **Chưa live** → quét lại mỗi 60 giây (đổi được trong Cài đặt), tới khi live thì quay.
3. Phòng đang được theo dõi hiện badge `CHỜ LIVE` hoặc `LIVE`, và `REC` khi đang quay.
4. **■ Dừng quay** kết thúc trọn vẹn: ngừng quay **và** ngừng theo dõi, thẻ trở về
   `CHƯA THEO DÕI` chỉ với **một lần bấm**. Không có hộp thoại xác nhận.

> **Vì sao một lần bấm là đủ:** nếu chỉ dừng tiến trình mà vẫn giữ theo dõi thì lần quét
> kế tiếp sẽ thấy phòng vẫn live và quay lại ngay — trước đây phải bấm 2–3 lần mới dừng hẳn.
> Giờ nút dừng tắt luôn theo dõi nên không thể quay lại. Muốn theo dõi tiếp thì bấm
> **Bắt đầu theo dõi**.
>
> Khi đang theo dõi mà chưa quay, nút chính là **Dừng theo dõi** (không có gì để dừng quay).

Trạng thái theo dõi **được lưu lại** — lần sau mở web, phòng bạn đã bật vẫn tiếp tục theo dõi.

Các nút khác trên thẻ phòng:

| Nút | Tác dụng |
|---|---|
| ⏺ | Quay ngay (chỉ hiện khi đang theo dõi và chưa quay) |
| 📋 | Xem nhật ký chi tiết (tiến độ, tốc độ, lỗi…) |
| ⚙ | Cài đặt riêng cho phòng: chất lượng, định dạng, giới hạn thời lượng, số bản giữ lại |

Khi **thêm phòng mới**, có ô tick **“Bắt đầu theo dõi ngay”**. Không tick thì phòng chỉ được thêm vào danh sách — bạn bấm theo dõi sau.

### Bản ghi

Tab **Bản ghi** liệt kê các video đã quay xong. Mỗi thẻ có:

| Nút | Tác dụng |
|---|---|
| ⬇ | Tải file về máy |
| ▶ | Xem trước ngay trong trình duyệt |
| ★ | Đánh dấu yêu thích — **bản yêu thích được miễn trừ tự xoá** |
| 🗄 | Chuyển vào `_archived` (không bị tự xoá) |
| 🗑 | Xoá vĩnh viễn cả bản ghi lẫn file |

---

## 3. Video được lưu ở đâu

Mặc định: **`D:\y-storage`**, chia theo nền tảng:

```
D:\y-storage\
├── bigo\
│   ├── anubdn2s_2026-09-19_14-30-05.mp4
│   └── khanhly_2026-09-19_21-05-40.mp4
├── tiktok\
│   └── someuser_2026-09-20_09-12-33.mp4
└── _archived\          ← các bản ghi bạn bấm "Lưu trữ"
```

**Quy tắc đặt tên:** `username_ngày_giờ.mp4`

`username` là nickname thật lấy từ nền tảng (không phải ID bạn nhập). Nếu chưa lấy được nickname thì dùng ID bạn nhập. Nếu trùng giây, TOOL tự thêm `_2`, `_3`… nên **không bao giờ ghi đè nhau**.

### Đổi thư mục lưu

Vào **Cài đặt → Chung → Thư mục lưu video**:

- Gõ đường dẫn trực tiếp, hoặc
- Bấm **Chọn…** để mở hộp thoại chọn thư mục thật của Windows
- Bấm **Lưu**

Thư mục `bigo` và `tiktok` sẽ được tạo tự động. **Không cần khởi động lại TOOL.**

> Thay đổi được ghi vào `config.json` ngay lập tức.

---

## 4. Cấu trúc thư mục

```
MyTool\
├── start.bat           ← click đúp để chạy
├── config.json         ← thư mục lưu video, cổng chạy
├── main.py             ← server (FastAPI)
├── bigo_engine.py      ← engine lấy link HLS Bigo (đã obfuscate, KHÔNG sửa)
├── index.html          ← toàn bộ giao diện (1 file duy nhất)
├── pick_folder.py      ← hộp thoại chọn thư mục Windows
├── requirements.txt
├── LICENSE             ← giấy phép MIT
├── data\               ← cài đặt, cookies (tự tạo khi chạy)
│   ├── state.json
│   └── cookies\
├── venv\               ← môi trường Python (tự tạo)
└── _backup\            ← code cũ giữ lại để tham khảo
    ├── legacy-m3u8-recorder\
    └── streamerREC-upstream-extras\
```

---

## 5. Cài đặt trong giao diện

### Chung

| Mục | Ý nghĩa |
|---|---|
| **Thư mục lưu video** | Nơi chứa `bigo\` và `tiktok\` |
| **Khoảng thời gian kiểm tra** | Bao lâu quét trạng thái live một lần, **chỉ với các phòng đang theo dõi**. Mặc định 60 giây |
| **Tự xoá bản cũ** | Xoá bản ghi cũ hơn N ngày. `0` = giữ mãi. Bản ★ được miễn |
| **Chế độ máy yếu** | Giảm CPU/RAM/IO cho máy cấu hình thấp. Bật là **có hiệu lực ngay** (không cần khởi động lại) |

Các mục khác trên thanh tiêu đề:

| Mục | Ý nghĩa |
|---|---|
| **Chưa quay** / **N đang quay** | Số phòng **đang quay ngay lúc này**. Rê chuột vào để xem tổng số phòng, số phòng đang theo dõi và số đang quay |
| **D:\y-storage** | Thư mục lưu video. Rê chuột để xem đường dẫn đầy đủ |

### Quay

| Mục | Ý nghĩa |
|---|---|
| **Chất lượng mặc định** | Cao nhất / 1080p / 720p / 480p / Thấp nhất |
| **Định dạng mặc định** | MP4 / MKV / TS |
| **Tự chuyển sang MP4** | Remux không mất chất lượng sau khi quay xong |
| **Thời lượng tối đa** | Tự dừng sau N phút. `0` = không giới hạn |
| **Tự dừng khi treo** | Dừng khi 2 phút không nhận được dữ liệu |

### Mạng

- **Proxy chung** — dùng khi Bigo/TikTok bị chặn ở mạng của bạn
- **Tự kết nối lại** — nối lại khi stream ngắt giữa chừng

### Thông báo

Dán **Discord webhook** hoặc URL nhận JSON để được báo khi phòng live / quay xong.

### Cookies

Cho stream giới hạn tuổi hoặc cần đăng nhập: xuất cookies bằng extension *Get cookies.txt LOCALLY*, rồi tải file `.txt` lên đây.

---

## 6. Bảo mật

**Không có đăng nhập** — không tài khoản, không mật khẩu, không phiên.

Ranh giới bảo mật là **địa chỉ lắng nghe**: mặc định TOOL chỉ nghe ở `127.0.0.1:5000`,
nên **chỉ máy bạn** mở được. Máy khác trong mạng LAN không truy cập được.

> Nếu bạn sửa `host` trong `config.json` thành `0.0.0.0` để dùng từ máy khác, **ai trong
> mạng cũng điều khiển được TOOL** — thêm/xoá phòng, tải và xoá video. Khi đó hãy tự đặt
> lớp bảo vệ bên ngoài (firewall, Tailscale, hoặc reverse proxy có xác thực).

---

## 7. Xử lý sự cố

**"Không tim thay Python"**
→ Cài Python và tick "Add Python to PATH", hoặc gỡ rồi cài lại.

**"Không tim thay ffmpeg"**
→ FFmpeg chưa có trong PATH. Xem lại mục 1.

**Thêm phòng báo "ID không hợp lệ"**
→ ID của bạn có dấu cách hoặc ký tự `/ \ ? # % & = +`. Hãy dán link đầy đủ thay vì ID.
Các dạng ID khác — kể cả bắt đầu bằng `_` `.` `-` hay có dấu tiếng Việt — đều được chấp nhận.

**Thêm phòng báo "Phòng này đã có trong danh sách rồi"**
→ TOOL chặn trùng để không quay 2 lần cùng một phòng. Xoá phòng cũ trước.

**Phòng hiện `LIVE` nhưng bấm quay báo "phòng đang live nhưng không lấy được link stream"**
→ Bigo đã từ chối token. TOOL tự thử lại 3 lần, mỗi lần **lấy token mới** (cách này quan trọng —
token cũ bị từ chối thì thử lại bằng chính nó là vô ích). Nếu vẫn thất bại, chờ vài phút rồi bấm lại;
Bigo thường chỉ chặn tạm thời. Xem nhật ký 📋 để thấy từng lần thử.

**Bấm quay mà thẻ báo `offline`**
→ Bigo báo phòng chưa live. Kiểm tra lại trên web xem phòng có thực sự đang phát không.

**Quay xong nhưng file 0 byte / file lỗi**
→ Thường do link HLS hết hạn giữa chừng. Bật **Tự kết nối lại** trong Cài đặt → Mạng.

**Phòng TikTok đang live nhưng thẻ vẫn báo `CHỜ LIVE` / `offline`, bấm quay là lỗi ngay**
→ TikTok chặn truy cập tự động bằng một cổng chống bot (WAF) trên `tiktok.com`: máy khách thường
nhận về một trang "Please wait…" thay vì trang phòng, nên các công cụ dựa trên yt-dlp báo sai là
"kênh chưa phát trực tiếp". TOOL **tự giải câu đố WAF này** rồi đọc trạng thái phòng từ chính
TikTok và lấy link HLS thật để quay, nên không cần cookie và không phụ thuộc yt-dlp cho TikTok nữa.
- Cần gói `curl_cffi` (đã có trong `requirements.txt`) — cứ cài lại `pip install -r requirements.txt`
nếu bạn từng cài từ bản cũ, rồi khởi động lại TOOL.
- Bấm **📋** trên thẻ phòng để xem nhật ký nếu vẫn lỗi; thông báo sẽ nói rõ là "phòng đang offline"
hay "không lấy được link stream".

**Thẻ phòng TikTok hiện tên dạng `@trlyday` thay vì biệt danh thật**
→ Phòng đã lưu từ bản cũ: lúc đó TOOL gặp WAF nên chỉ lấy được tên từ link. TOOL sẽ tự cập nhật
lại biệt danh, avatar và tiêu đề ngay lần đầu đọc được phòng (khi bấm **Bắt đầu theo dõi**, hoặc
khi phòng live). Muốn thấy ngay thì xoá phòng rồi thêm lại.

**Phòng không xuất hiện trong làn nào**
→ Phòng được xếp làn theo `platform`. Kiểm tra tên nền tảng trong **Cài đặt phòng** — nếu là nền
tảng lạ, nó nằm ở làn **Nền tảng khác** (làn này tự hiện khi có phòng như vậy).

**Quay được bao nhiêu phòng cùng lúc?**
→ **Không có giới hạn cứng.** Số phòng quay đồng thời chỉ phụ thuộc CPU, dung lượng đĩa và băng thông.
Con số `6` (hoặc `3` khi bật Chế độ máy yếu) chỉ giới hạn các **tiến trình phụ ngắn hạn** — dò
metadata, kiểm tra live, remux, gửi webhook — chứ không giới hạn việc quay.

---

## 8. Ghi chú

- Mặc định **không phòng nào được theo dõi**. Bạn bấm “Bắt đầu theo dõi” trên từng thẻ phòng. Không có gì tự chạy khi mở web.
- Bảng phòng chia **2 làn cố định: TikTok bên trái, Bigo bên phải** (làn thứ ba chỉ hiện khi có nền tảng khác). Hai nền tảng không bao giờ trộn lẫn.
- `bigo_engine.py` là file đã được **obfuscate có chủ đích** bởi tác giả dự án gốc, nhằm giấu thuật toán vượt cổng chống bot của Bigo. Nội dung sau khi giải mã là engine hợp lệ: gọi API Bigo, lấy link HLS, và chạy proxy nội bộ để giải mã `EXT-X-BIGO-WEB-PROTECTION`. Nó **không chứa mã độc**. **Đừng sửa file này.**
- **TikTok** không đi qua yt-dlp nữa: TOOL tự giải câu đố chống bot (WAF) của `tiktok.com`, đọc trạng thái phòng (`SIGI_STATE`) và hỏi API `webcast/room/info` để lấy link HLS, sau đó đưa link HLS đó cho yt-dlp tải. Nhờ vậy việc **dò live, thêm phòng và quay TikTok** đều hoạt động dù TikTok chặn máy khách thường. Phần này cần `curl_cffi` (để giả lập dấu vân tay TLS của trình duyệt); nếu thiếu, TOOL vẫn thử bằng `requests` và ghi cảnh báo trong nhật ký.
- Công cụ này dựa trên dự án mã nguồn mở [StreamRec](https://github.com/orhogi/streamerREC) của **orhogi**, phát hành theo giấy phép **MIT** (xem `LICENSE`).
- Chỉ dùng để quay các phòng mà bạn có sự cho phép hoặc thoả thuận với chủ phòng.
