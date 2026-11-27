# LINE Integration Phase 1 — Inbox adapter (Issue #2, JIHO)

```
ลูกค้า LINE ─▶ gpr-line-webhook (Cloudflare Worker เดิม, ไม่แก้) ─▶ KV: gpr-line-inbox (LINE_INBOX)
                                                                       │ อ่านอย่างเดียว (REST API)
                                                                       ▼
                           gpr.line_inbox pull ─▶ GPR_DATA_DIR/<env>/outbox.jsonl  (private, นอก repo)
                                                                       │ JSON กลาง (schemas/gpr_inbox_message.schema.json)
                                                                       ▼
                    Bo: แปล/ดึงข้อมูลการจอง ─▶ gpr.handoff (เรียก gpr.pricing เดิม) ─▶ รายงาน Gift ─▶ Gift ตอบเองใน LINE OA
```

## สิ่งที่ระบบนี้ **ไม่ทำ**
ไม่สร้าง webhook ใหม่ · ไม่แก้ Worker/KV · ไม่ตอบ LINE อัตโนมัติ · ไม่ยืนยันคิว/การชำระเงิน (`reply_allowed`, `booking_confirmed`, `payment_confirmed` เป็น `false` เสมอ) · ไม่เก็บข้อมูลลูกค้า/Token ใน repo

## ตั้งค่า (บนเครื่องที่รัน adapter — ค่าทั้งหมดเป็น environment variable)
| ตัวแปร | ใช้เมื่อ | หมายเหตุ |
|---|---|---|
| `CF_ACCOUNT_ID` | `--source kv` | Cloudflare dashboard → Account ID |
| `CF_KV_NAMESPACE_ID` | `--source kv` | ID ของ namespace `gpr-line-inbox` (ไม่ใช่ชื่อ binding) |
| `CF_API_TOKEN` | `--source kv` | **สร้าง token ใหม่สิทธิ์ต่ำสุด:** Account → Workers KV Storage → **Read** เท่านั้น จำกัดเฉพาะ account นี้ ตั้งวันหมดอายุ |
| `GPR_ID_SALT` | `--env prod` | สุ่มยาว ≥ 16 ตัว ใช้ทำรหัสลูกค้าแบบนามแฝง (`cust_…`) เปลี่ยนแล้วรหัสลูกค้าจะเปลี่ยนทั้งหมด |
| `GPR_ADMIN_USER_IDS` | แนะนำ | LINE userId ของ Gift — คำสั่ง `#GPR …` จากเมนูผู้ดูแลนับเฉพาะจากบัญชีนี้ |
| `GPR_TEST_USER_IDS` | แนะนำ | LINE userId ของ Gift/บัญชีทดสอบ คั่นด้วย `,` → ข้อความจากบัญชีนี้ไปที่ `test` เสมอ แม้รันแบบ prod |
| `GPR_DATA_DIR` | ไม่บังคับ | ค่าเริ่มต้น `~/.gpr` (ต้องอยู่นอก repo) |

ห้ามวาง token ในแชท/Issue/ไฟล์ใน repo · Windows: ใช้ `setx` หรือ Windows Credential Manager

## รัน
```bash
# 0) ตรวจการตั้งค่า: บอกแค่ set/missing ไม่แสดงค่าลับ + ลองอ่าน KV 1 ครั้ง
python -m gpr.line_inbox check
# ทดสอบด้วยข้อมูลจำลอง (ไม่ต้องมี token)
python -m gpr.line_inbox pull --env test --source file --path tests/fixtures/line_webhook_synthetic.json
# ของจริง (read-only)
python -m gpr.line_inbox pull --env prod --source kv --limit 200
# ทดสอบกับ KV ต้องระบุ prefix ของ key ทดสอบเสมอ
python -m gpr.line_inbox pull --env test --source kv --prefix <test-prefix>
# ต่อราคา: Bo เขียน booking.json ตาม CHAT_PLAYBOOK ข้อ 3 แล้ว
python -m gpr.handoff --record rec.json --booking booking.json
```

## การกันข้อความซ้ำ
- คีย์: `line:evt:<webhookEventId>` (LINE ส่ง id เดิมเมื่อ redelivery) → สำรอง `line:msg:<message.id>` → `line:sha:<hash>`
- เก็บใน `GPR_DATA_DIR/<env>/state.sqlite` แยก test/prod · รันซ้ำกี่ครั้งก็ไม่เพิ่มรายการซ้ำ
- adapter ไม่ลบ key ใน KV (read-only) — ควรตั้ง TTL ใน Worker ภายหลังถ้าต้องการลดข้อมูลค้าง (ต้องให้ Gift อนุมัติ)

## รูปภาพ
บันทึกแค่ `message.id` + ผู้ให้บริการเนื้อหา · ไฟล์ภาพต้องดึงจาก LINE Content API ด้วย channel token ฝั่งเซิร์ฟเวอร์เท่านั้น (ยังไม่ทำใน Phase 1) · ทุกภาพติดธง `image_needs_human_check` — **ภาพสลิปเป็นหลักฐานรอตรวจ ไม่ใช่การยืนยันชำระเงิน**

## รูปแบบข้อมูลใน KV จริง (จาก worker.js ของ BO)
- key: `event:<webhookEventId>` · อายุ 30 วัน (`expirationTtl: 2592000`) · LINE ส่งซ้ำ (redelivery) จะเขียนทับ key เดิม
- value: `{"id", "timestamp", "source", "message": {"id", "type", "text"?}}` — เก็บเฉพาะ event ประเภท message
- Worker ตรวจ `x-line-signature` (HMAC-SHA256) ก่อนบันทึก ✅ · ไม่มีโค้ดตอบกลับ ✅
- adapter แปลง record นี้เป็น event มาตรฐานก่อน normalise · ข้ามการอ่าน value ของ key ที่ประมวลผลแล้ว (ประหยัด KV reads)

### ทดสอบกับ KV จริง
Worker เขียนข้อความทดสอบและข้อความลูกค้าลง prefix เดียวกัน (`event:`) จึงแยกด้วยผู้ส่ง:
`--env test --source kv` เก็บ **เฉพาะ** event จาก `GPR_TEST_USER_IDS` และทิ้งที่เหลือโดยไม่บันทึก

### โควต้า Cloudflare (แผนฟรี — ตรวจตัวเลขล่าสุดใน dashboard)
การ list key ใน KV มีโควต้ารายวัน (แผนฟรีประมาณ 1,000 ครั้ง/วัน) → **อย่าดึงถี่กว่าทุก 2 นาที** หรือดึงเมื่อกดคำสั่ง/ตามรอบงาน

### ข้อสังเกตต่อ Worker (ไม่ได้แก้ — ให้ BO/Gift ตัดสินใจ)
1. ถ้า LINE ส่ง event ที่ไม่มี `webhookEventId` จะถูกข้าม (ปกติ LINE ใส่ทุก event — ไม่เป็นปัญหา)
2. ข้อความประเภท follow/unfollow/postback ไม่ถูกเก็บ — ถ้าต่อไปอยากรู้ว่าใครเพิ่มเพื่อน ต้องเพิ่มใน Worker
3. ถ้า `put` ล้มกลางทาง Worker ตอบ 503 → ควรเปิด **Webhook redelivery** ใน LINE Developers Console เพื่อให้ LINE ส่งซ้ำ (key เดิม ไม่ซ้ำ)

## Rich Menu ผู้ดูแล (แบบของ BO, 6 ช่อง)
ไฟล์: `line/richmenu/richmenu.json` + ภาพ `line/richmenu/richmenu-admin.jpg` (แปลงจากแบบของ BO เป็น 2500×1686 JPEG ≈ 0.5 MB)

| แถวบน | แปลแชท | ลูกค้า | เช็กคิว |
|---|---|---|---|
| **แถวล่าง** | **รับเงิน** | **คำนวณราคา** | **สรุปงาน** |

- เป็น **เมนูเครื่องมือของ Gift** ไม่ใช่เมนูลูกค้า → ผูกกับ LINE userId ของ Gift เท่านั้น (`--link-admin`) **ห้ามตั้งเป็นเมนูเริ่มต้น** ไม่งั้นลูกค้าทุกคนจะเห็นปุ่ม "รับเงิน/สรุปงาน"
- กดแล้วส่งข้อความ `#GPR <คำสั่ง>` เข้าแชท OA → เข้า inbox → adapter ติด `admin_command` (translate_chat, customer_lookup, check_schedule, payment_review, calculate_price, daily_summary) **เฉพาะเมื่อผู้ส่งอยู่ใน `GPR_ADMIN_USER_IDS`** ถ้าลูกค้าพิมพ์เลียนแบบ → ติดธง `admin_command_from_non_admin_or_unknown`
- ไม่มีบอทตอบกลับใน LINE: BO/JIHO ทำงานตามคำสั่งแล้วแสดงผลบนเครื่อง · `payment_review` = เปิดรายการสลิปรอตรวจ ไม่ใช่ยืนยันเงิน
- ภาพถูกขยายจาก 1528×1029 → ภาพอาจเบลอเล็กน้อย ถ้า BO ส่งออกที่ 2500×1686 ได้จะคมกว่า
- ตรวจ: `python -m gpr.richmenu validate line/richmenu/richmenu.json`
- ใช้จริง (Gift รันเอง, เปลี่ยน LINE production):
  `LINE_CHANNEL_ACCESS_TOKEN=… GPR_ADMIN_USER_ID=… python -m gpr.richmenu apply line/richmenu/richmenu.json --image line/richmenu/richmenu-admin.jpg --link-admin`
- เมนูสำหรับลูกค้า (ถ้าต้องการ) เป็นอีกชุดแยกต่างหาก ยังไม่ได้ทำ
