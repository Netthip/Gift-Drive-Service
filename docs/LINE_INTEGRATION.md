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
| `GPR_TEST_USER_IDS` | แนะนำ | LINE userId ของ Gift/บัญชีทดสอบ คั่นด้วย `,` → ข้อความจากบัญชีนี้ไปที่ `test` เสมอ แม้รันแบบ prod |
| `GPR_DATA_DIR` | ไม่บังคับ | ค่าเริ่มต้น `~/.gpr` (ต้องอยู่นอก repo) |

ห้ามวาง token ในแชท/Issue/ไฟล์ใน repo · Windows: ใช้ `setx` หรือ Windows Credential Manager

## รัน
```bash
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

## ยังไม่ทราบ (ต้องให้ Gift/Bo ยืนยัน)
1. Worker เก็บอะไรใน KV จริง: key pattern และ value เป็น webhook body ทั้งก้อนหรือ event เดี่ยว — adapter รองรับ `{events:[…]}`, event เดี่ยว, และห่อแบบ `{event|body|payload|data: …}` (รวม string JSON) ถ้าเป็นแบบอื่นจะนับเป็น `unparsed` และไม่ทำอะไรต่อ
2. key ทดสอบมี prefix อะไร (ใช้กับ `--prefix`)

## Rich Menu (ร่าง 6 ช่อง)
`line/richmenu/richmenu.json` — 2500×1686 แบ่ง 3×2 · ปุ่มเป็น message action (กดแล้วข้อความเข้า inbox ให้คนตอบ ไม่มีบอทตอบเอง)
**ข้อความ/ชื่อปุ่มยังเป็นร่าง รอแบบจาก Bo** · ตรวจ: `python -m gpr.richmenu validate line/richmenu/richmenu.json`
ใช้จริง (เปลี่ยนบัญชี LINE production — Gift รันเองหลังอนุมัติแบบและภาพ 2500×1686):
`LINE_CHANNEL_ACCESS_TOKEN=… python -m gpr.richmenu apply line/richmenu/richmenu.json --image menu.png [--set-default]`
