# 1Plat Payment Integration

## Overview
This document describes the 1Plat payment integration for the SPN VPN Bot. 1Plat is a payment processing service that supports card and SBP (Russian fast payment system) payments.

## Configuration

### Environment Variables
Add these variables to your `.env` file:

```env
# 1Plat Payment Configuration
ONEPLAT_SHOP_ID=1374
ONEPLAT_SHOP_SECRET=PLT5FWL2AUNC78T76UIOPUYU449KD2PI
ONEPLAT_WEBHOOK_URL=https://spn.bot.idlebat.online/1plat-webhook
```

## Payment Flow

### 1. User Initiates Payment
- User selects a tariff (1m, 3m, 6m, or 12m)
- User chooses payment method: **1Plat (карта/СБП)**
- User selects payment type: **Card (💳)** or **SBP (📱)**

### 2. Create Payment Order
The bot calls the 1Plat API to create a payment order:
- **Endpoint**: `POST /api/merchant/order/create/by-api`
- **Authentication**: Uses `sign` (MD5 signature of request)
- **Signature**: `MD5(shopId:secret:amount:merchantOrderId)`

### 3. Display Payment Details
Based on payment method:
- **Card**: Shows card number, bank name, and FIO for transfer
- **SBP**: Shows QR code and payment link

### 4. Payment Confirmation
Two methods:

#### Method A: Manual Check
- User clicks "✅ Проверить оплату" button
- Bot calls `GET /api/merchant/order/info/{guid}/by-api`
- If `status == 1 or 2` (paid/confirmed), activates subscription

#### Method B: Automatic via Webhook
- 1Plat sends callback to `https://spn.bot.idlebat.online/1plat-webhook`
- Webhook verifies signature using `signature_v2`
- On successful verification, activates subscription

### 5. Subscription Activation
Once payment is confirmed:
1. Creates/extends user in Remnawave
2. Adds user to default squad
3. Processes referral bonuses (if applicable)
4. Sends subscription link to user
5. Updates database payment status to 'paid'

## API Endpoints

### Create Payment
```
POST https://1plat.cash/api/merchant/order/create/by-api

Request:
{
  "merchant_order_id": "spn_123456_1704067200",
  "user_id": "123456",
  "amount": 100,
  "email": "123456@temp.com",
  "method": "card" | "sbp",
  "sign": "md5_hash"
}

Response:
{
  "success": 1,
  "guid": "uuid-string",
  "payment": {
    "note": {
      "currency": "RUB",
      "pan": "2200 1545 3449 7549",
      "bank": "Альфа",
      "fio": "Егор Гончаров"
    },
    "method_group": "card",
    "status": 0,
    "amount_to_pay": 100
  }
}
```

### Get Payment Info
```
GET https://1plat.cash/api/merchant/order/info/{guid}/by-api

Headers:
  x-shop: 1374
  x-secret: PLT5FWL2AUNC78T76UIOPUYU449KD2PI

Response:
{
  "success": 1,
  "payment": {
    "id": 271097,
    "guid": "uuid-string",
    "status": 1 | 2,  // 1 = paid, 2 = confirmed
    "amount": 100,
    "note": { ... }
  }
}
```

### Webhook Callback
```
POST https://spn.bot.idlebat.online/1plat-webhook

Request:
{
  "signature": "sha256_hmac",
  "signature_v2": "md5_hash",
  "payment_id": "123",
  "guid": "uuid-string",
  "merchant_id": "1374",
  "user_id": "123456",
  "status": 1,
  "amount": 100,
  "amount_to_pay": 100,
  "amount_to_shop": 85
}

Response:
{
  "status": "ok"
}
```

## Payment Status Codes

| Status | Meaning |
|--------|---------|
| -2 | No suitable payment method found |
| -1 | Draft (waiting for method selection) |
| 0 | Pending payment |
| 1 | Paid (pending merchant confirmation) |
| 2 | Paid and confirmed (complete) |

## Signature Verification

### For Callbacks (signature_v2)
```python
import hashlib

def verify_signature_v2(merchant_id, amount, shop_id, signature_v2, secret):
    data = f"{merchant_id}{amount}{shop_id}{secret}"
    expected = hashlib.md5(data.encode()).hexdigest()
    return signature_v2 == expected
```

### For API Requests (sign)
```python
import hashlib

def generate_sign(shop_id, secret, amount, merchant_order_id):
    data = f"{shop_id}:{secret}:{amount}:{merchant_order_id}"
    return hashlib.md5(data.encode()).hexdigest()
```

## File Structure

### New/Modified Files
- `services/oneplat.py` - 1Plat API integration service
- `config.py` - Added 1Plat configuration variables
- `database.py` - Added migration for guid field
- `handlers/subscription.py` - Added 1Plat payment handlers and UI
- `main.py` - Added webhook endpoint server

### Key Functions

**services/oneplat.py:**
- `create_oneplat_payment()` - Create payment order
- `get_payment_info()` - Get payment status
- `process_paid_payment()` - Activate subscription after payment
- `handle_oneplat_callback()` - Process webhook callbacks
- `verify_callback_signature_v2()` - Verify callback authenticity

**handlers/subscription.py:**
- `process_pay_oneplat()` - Show payment method selection (card/sbp)
- `process_oneplat_payment()` - Create payment and show details
- `process_check_oneplat_payment()` - Check payment status manually

## Webhook Setup

The bot runs a web server on port 8080 that listens for 1Plat callbacks:

```
http://localhost:8080/1plat-webhook
```

For production, this is exposed via Nginx at:
```
https://spn.bot.idlebat.online/1plat-webhook
```

Configure this URL in your 1Plat shop settings → Webhooks.

## Security Considerations

1. **Signature Verification**: Always verify callbacks using `signature_v2`
2. **HTTPS Only**: Use HTTPS for webhook URL in production
3. **Database Locking**: Uses PostgreSQL advisory locks to prevent race conditions
4. **Idempotency**: Webhook can be received multiple times safely (already-processed payments are skipped)
5. **Secret Key**: Store `ONEPLAT_SHOP_SECRET` securely in environment variables

## Testing

### Manual Payment Test Flow
1. User presses "Оформить подписку" → "Выбери срок подписки"
2. Selects tariff (e.g., "1 месяц — 100₽")
3. Selects "💳 1Plat (карта/СБП)"
4. Chooses payment method: "💳 Банковская карта" or "📱 СБП"
5. Bot displays payment details
6. Test payment in 1Plat test environment
7. Verify with "✅ Проверить оплату" or wait for webhook
8. Confirm subscription activated with link sent

### Webhook Test
```bash
curl -X POST https://spn.bot.idlebat.online/1plat-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "signature": "test",
    "signature_v2": "test",
    "payment_id": "123",
    "guid": "test-guid",
    "merchant_id": "1374",
    "user_id": "123456",
    "status": 1,
    "amount": 100,
    "amount_to_pay": 100,
    "amount_to_shop": 85
  }'
```

## Deployment Notes

### Server Configuration
- Ensure port 8080 is open (or your configured webhook port)
- Use Nginx reverse proxy for HTTPS termination
- Configure firewall to allow 1Plat webhook requests

### Database Migration
The migration adds a `guid` column to the `payments` table:
```sql
ALTER TABLE payments ADD COLUMN IF NOT EXISTS guid VARCHAR(255);
```

This happens automatically on bot startup.

## Troubleshooting

### Webhook Not Received
1. Check webhook URL in 1Plat settings
2. Verify firewall allows connections to webhook port
3. Check bot logs for webhook handler errors
4. Test webhook endpoint is accessible

### Payment Not Confirming
1. Check signature verification (enabled by default)
2. Verify `ONEPLAT_SHOP_ID` and `ONEPLAT_SHOP_SECRET` are correct
3. Check payment status in 1Plat dashboard
4. Manually trigger with "Проверить оплату" button

### Referral Bonus Not Applied
1. Verify referrer has Remnawave UUID
2. Check that referrer_id is set correctly in database
3. Verify first_payment flag logic in `process_paid_payment()`

## Support
For 1Plat API issues, consult: https://1plat.cash/doc/api
For bot issues, contact: support@spn-vpn.bot
