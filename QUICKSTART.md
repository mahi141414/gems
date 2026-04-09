# 🚀 QUICK START GUIDE

## 5 Minute Setup

### Step 1: Get Your Cookies
1. Open https://gemini.google.com
2. Login with your Google account
3. Press **F12** (Developer Tools)
4. Go to **Application** → **Cookies** → **https://gemini.google.com**
5. Copy the values of:
   - `__Secure-1PSID` ✓
   - `__Secure-1PSIDTS` (may be empty)

### Step 2: Create Credentials File
Save these as `cookies.json`:

```json
{
  "__Secure-1PSID": "paste_your_psid_here",
  "__Secure-1PSIDTS": "paste_your_psidts_here"
}
```

### Step 3: Run the Server

**Windows:**
```bash
double-click run.bat
```

**macOS/Linux:**
```bash
bash run.sh
```

### Step 4: Use the API

Open browser: **http://localhost:8000/docs**

Or try a quick request:
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello!", "stream": false}'
```

---

## Common Issues & Fixes

### Issue: "Credentials not configured"
**Fix:** Make sure `cookies.json` exists with your actual cookie values

### Issue: "Connection refused"
**Fix:** Make sure server is running (`python main.py` or `run.bat`)

### Issue: Server won't start
**Fix:** Delete `venv` folder and run `run.bat` or `run.sh` again

---

## What You Can Do

✅ Chat with Gemini  
✅ Generate images  
✅ Analyze files  
✅ Deep research  
✅ Create custom AI personalities  
✅ Stream responses for real-time UI

---

## API Examples

### Python
```python
import requests

# Generate text
resp = requests.post("http://localhost:8000/generate", 
    json={"prompt": "Hello!"})
print(resp.json()["text"])

# Start chat
resp = requests.post("http://localhost:8000/chat/start",
    json={"prompt": "Hi there!"})
chat_id = resp.json()["session_id"]

# Continue chat
resp = requests.post("http://localhost:8000/chat/reply",
    json={"session_id": chat_id, "prompt": "Tell me more"})
print(resp.json()["text"])
```

### JavaScript
```javascript
// Generate text
const resp = await fetch("http://localhost:8000/generate", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({prompt: "Hello!"})
});
const data = await resp.json();
console.log(data.text);
```

### cURL
```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Python?"}'
```

---

## Full Documentation

See **README.md** for complete documentation

Enjoy! 🎉
