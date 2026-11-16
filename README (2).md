# 🤖 JARVIS - Telegram AI Bot (Groq + Render)

Ek hi `bot.py` me poora Telegram bot: kahani wali video (photo + Hindi awaaz + music), mausam ka graphic card,
code se animation, QR, meme, photo effects aur 40 se zyada commands. Sab kuchh free models aur free APIs par chalta hai.
Saari settings website ke `/admin` page se badalti hain, code chhedne ki zaroorat nahi.

---

## 1. Files

| File | Kaam |
|---|---|
| `bot.py` | Poora bot + website (`/health`, `/admin`) |
| `requirements.txt` | Python packages |
| `Dockerfile` | Render par chalane ka tarika (cairo, fonts, raqm install karta hai) |
| `README.md` | Ye guide |

Dhyan rakhein: GitHub me teeno files **repo ke root me** hon aur naam bilkul `bot.py` ho (`bot.by` nahi).

---

## 2. Render par lagana (pehli baar)

1. GitHub par repo banayein aur ye files upload karein.
2. Render → **New → Web Service** → apna repo chunein.
3. **Language: Docker**, Branch: `main`, Plan: **Free**.
4. **Environment Variables** me sirf ek cheez daalein: `ADMIN_PASSWORD` = aapka koi mazboot password.
5. **Advanced → Health Check Path** = `/health`.
6. **Deploy web service** dabayein (5-8 minute).
7. `https://aapka-app.onrender.com/admin` kholein, password daalein, aur bharein:
   - **Telegram Bot Token** (@BotFather se)
   - **Groq API Key** (console.groq.com se)
   - **Allowed Telegram IDs**: bot ko `/id` bhejein, jo number aaye wo yahan daalein
8. **Save** dabayein. Upar "Telegram bot: Chalu" dikhna chahiye.

### Code badalne par update
GitHub me file commit karein → Render khud deploy karta hai. Na kare to **Manual Deploy → Deploy latest commit**.

### Settings restart par bani rahein
Free Render par settings file restart par mit jaati hai. Isse bachne ke liye Render me **Account Settings → API Keys**
se key banakar `RENDER_API_KEY` naam se Environment me daalein. Tab har Save Render ke Environment me bhi chala jata hai.

### 24/7 chalu rakhna
Bot har 10 minute me khud ko `/health` par ping karta hai. Pakka karne ke liye UptimeRobot (free) se
`https://aapka-app.onrender.com/health` ko har 5 minute me ping karwayein.

---

## 3. Admin page ki settings (`/admin`)

| Setting | Matlab |
|---|---|
| Groq Model (main) | Default `openai/gpt-oss-120b`. `auto` likhein to bot khud sabse powerful available model chunta hai |
| Backup model | Main fail ya rate-limit ho to ye chalta hai (default `openai/gpt-oss-20b`) |
| Voice-to-text model | Voice message samajhne ke liye (`whisper-large-v3-turbo`) |
| Allowed Telegram IDs | Kaun bot use kar sakta hai (khaali = sab) |
| Bot ka behaviour | Chat ka system prompt |
| Default bhasha / shehar | Kahani, mausam card aur awaaz ke liye |
| Upstash Redis URL + Token | Yaaddasht hamesha ke liye (neeche section 6 dekhein) |
| Pexels / Pixabay key | Optional. Free key se photo search behtar. Na ho to Openverse chalta hai |
| Awaaz ki raftaar, voices | Hindi/English, purush/mahila awaaz badal sakte hain |
| Music volume | Background music kitni tez ho |
| Video width/height/FPS/seconds | Jyada FPS = smooth video, par free Render par der lagti hai |

Page ke upar status me dikhta hai: bot chalu hai ya nahi, Groq key set hai ya nahi, aur Groq account me kaun se models available hain.

---

## 4. Saare commands

### 🎬 Banane wale
| Command | Kya karta hai |
|---|---|
| `/story <vishay>` | Bot buttons se 5 sawal puchta hai (bhasha, prakar, parts, visuals, awaaz). Phir title card + har scene (2-3 alag shots, Hindi awaaz, music, subtitles) + end card, parts me bhejta hai aur poora joda hua video bhi |
| `/story <vishay> \| msg: Raman ko janamdin mubarak` | End card par aapka sandesh likha aur bola jata hai |
| `/video <vivaran>` | LLM ka likha code har frame alag banata hai (smooth animation) + music. Sthir (static) nikle to bot khud dobara likhwata hai |
| `/video <vivaran> \| msg: Happy Diwali` | Video ke upar aapka message fade-in hota hai |
| `/wish birthday \| <sandesh>` | Animated wish video: gire confetti, text pop-in, awaaz aur music. Styles: `birthday`, `diwali`, `love`, `snow`, `bubbles`, `stars` |
| `/img <vivaran>` | Code se vector image (PNG + SVG) |
| `/photo <shabd>` | Internet se real photo dhundh kar bhejta hai (credit ke saath) |
| `/weather <shehar>` | Mausam ka graphic card (neeche dekhein) |
| `/quote <vishay>` | Photo background wala quote poster |
| `/meme upar \| neeche \| photo-shabd` | Meme. Kisi photo par reply karke bhi chalta hai |
| `/chart Bikri: Jan=10, Feb=25` | Bar chart image |
| `/qr <text ya link>` | QR code (PNG) |
| `/wifi naam \| password \| WPA` | WiFi QR, scan karte hi WiFi se jud jata hai |
| `/music <mood> [second]` | Code se music. Moods: calm, happy, sad, tense, epic, mystery |
| `/tts <text>` | Text ko awaaz me badalta hai |
| `/pdf Title \| text` | Text se PDF (Hindi sahit) |

### 🧰 Tools
| Command | Kya karta hai |
|---|---|
| `/calc 25*4+10`, `/calc 20% of 500` | Surakshit calculator |
| `/convert 10 km to miles` | Units (lambai, vajan, tapman, litre) |
| `/currency 100 usd inr` | Live currency rate |
| `/emi 500000 9 5` | Loan EMI (rashi, byaaj %, saal) |
| `/age 2000-05-14` | Umar aur agla janamdin |
| `/news [vishay]` | Taaza khabrein |
| `/wiki <vishay>` | Wikipedia saaransh (Hindi likhne par Hindi Wikipedia) |
| `/time <shehar>` | Kisi bhi shehar ka abhi ka samay |
| `/summarize <link ya text>` | Lamba text ya article ka saaransh |
| `/translate hindi: text` | Anuvaad |
| `/remind 10m dawa lena` | Yaad dilata hai (Upstash ho to restart ke baad bhi aata hai) |
| `/note`, `/notes`, `/delnote 2` | Chhote notes (Upstash ho to hamesha bache rehte hain) |
| `/remember <baat>`, `/memory`, `/forget <number\|summary\|all>` | Bot ki yaaddasht dekhna, jodna, hatana |
| `/pass [lambai]`, `/pick a, b, c`, `/dice`, `/coin` | Password, chunav, pasa, sikka |
| `/shayari`, `/joke`, `/fact` | Manoranjan |
| `/code <kaam>` | Poora code file me |

### 📷 Photo aur 🎤 Voice
- Photo bhejein → 10 effects ke buttons (B&W, Sepia, Blur, Sharp, Sketch, Cartoon, Pixel, Invert, Mirror, Vivid).
  Caption me `meme upar | neeche` likhein to seedha meme ban jata hai.
- Voice message bhejein → bot samajhta hai (Groq Whisper) aur jawab deta hai. `/voice on` se jawab awaaz me bhi aate hain.

### 💬 Seedha likhein (command ke bina)
- `Mumbai ka mausam` → mausam card
- `100 usd to inr` → currency
- `qr banao https://example.com` → QR
- `ek chhote bachhe ki kahani banao` → story wizard

### 🔧 Madad
`/diag` (jaanch: font, awaaz, QR, ffmpeg, yaaddasht, available models), `/id`, `/reset`, `/voice on|off`, `/help`

---

## 5. 🌦️ Mausam graphic kaise banta hai

`/weather Delhi` (ya "Delhi ka mausam") ye karta hai:

1. **Shehar → location:** Open-Meteo geocoding API (free, bina key).
2. **Mausam data:** Open-Meteo forecast API se abhi ka tapman, mehsoos hone wala tapman, nami, hawa, din/raat, aur agle 5 din ka forecast.
3. **Graphic:** Pillow se poora code me banta hai, koi AI image nahi:
   - Mausam ke hisaab se aasman ka gradient (saaf din, raat, baadal, baarish, tufaan, barf, kohra)
   - Bada icon (suraj, chaand, baadal, baarish, barf, bijli) shapes se drawn
   - Bada tapman, shehar ka naam, mausam ka naam (Hindi ya English, `DEFAULT_LANG` ke hisaab se)
   - Neeche 5 din ke panel: din ka naam, chhota icon, max/min tapman, baarish ki sambhavna
4. Card photo ke roop me aur PNG file ke roop me bheja jata hai.

Naya mausam kaisa dikhe, ye badalna ho to `bot.py` me `SKY` (rang), `weather_icon()` (icons) aur `weather_card()` (layout) dekhein.
Default shehar `/admin` me **Default shehar** se badlein.

---

## 6. 🧠 Yaaddasht (Memory): bot baatein bhoolta nahi

Bot teen tarah se yaad rakhta hai:

1. **Baatcheet:** pichli baatcheet har chat ke liye save hoti hai, to restart ke baad bhi baat wahin se aage badhti hai.
2. **Aapke baare me baatein:** har kuch messages ke baad bot background me naam, shehar, kaam, pasand, chal rahe projects jaisi
   sthayi baatein nikaal kar save karta hai (max 40). Har jawab me ye baatein bot ke paas hoti hain.
3. **Saaransh:** baatcheet lambi ho jaye to purani baatein ka chhota saaransh bana kar rakhta hai.

Password, OTP, card/bank/Aadhaar jaise numbers **kabhi save nahi hote**.

| Command | Kaam |
|---|---|
| `/remember meri behen ka naam Priya hai` | Khud kuchh yaad rakhwana |
| `/memory` | Bot ko aapke baare me kya yaad hai dekhna |
| `/memory off` / `/memory on` | Apne aap seekhna band/chalu |
| `/forget 2` / `/forget summary` / `/forget all` | Ek baat / saaransh / sab kuchh bhulwana |
| `/reset` | Sirf pichli baatcheet saaf (yaad rakhi baatein bani rehti hain) |

### Hamesha ke liye yaaddasht (Upstash, free)
Free Render par file restart par mit jaati hai. Isliye ek free Redis database jodein:

1. **upstash.com** par free account banayein.
2. **Create Database** (Redis), koi bhi naam, region India ke paas (jaise Mumbai) chunein, Free plan.
3. Database kholein, **REST API** section me `UPSTASH_REDIS_REST_URL` aur `UPSTASH_REDIS_REST_TOKEN` dikhenge.
4. Render me service ke **Environment** me ye do variables jodein (naam ye hi rakhein):
   - `UPSTASH_URL` = REST URL wali value
   - `UPSTASH_TOKEN` = REST Token wali value
   (Ya `/admin` page par "Upstash Redis REST URL/Token" me daalkar Save karein. Par Render restart ke baad wo tabhi bacha rahega jab ye Environment me bhi ho, ya `RENDER_API_KEY` set ho.)
5. Save/Deploy ke baad `/admin` me "Yaaddasht: Upstash Redis (hamesha ke liye)" dikhna chahiye. `/diag` me bhi.

Upstash jodne par ye sab bacha rehta hai: baatcheet, yaad rakhi baatein, notes, reminders, aur `/admin` ki saari settings
(token, keys, model...). Dhyan rakhein ki settings (keys sahit) aapke apne Upstash database me save hoti hain.

---

## 7. Story/video kaise bante hain

- **Kahani:** Groq model JSON me kahani likhta hai (har scene me 3 vaakya + har vaakya ka alag drishya).
- **Visuals (aap chunte hain):** real photos (Pexels/Pixabay/Openverse), vector art (LLM ka SVG), dono mix, ya poori code animation.
- **Awaaz:** `edge-tts` ki Hindi/English neural awaaz, har vaakya alag bana kar beech me chhota pause dekar joda jata hai (zyada natural). Na chale to gTTS.
- **Music:** numpy se code me bana, mood ke hisaab se chords aur arpeggio.
- **Video:** Ken-Burns zoom/pan, shots ke beech crossfade, vaakya-wise subtitles (Hindi shaping ke liye Pillow + raqm + Noto font).
- **Joda hua video:** title card + scenes + end card, ffmpeg se.

---

## 8. Limits aur dhyan rakhne wali baatein

- **Free Render me 0.1 CPU** milta hai. Isliye `/story` banne me kai minute lag sakte hain. Tez chahiye to paid plan ($7) lein ya `/admin` me video width/FPS kam karein.
- **Groq free limits:** lambi kahani par rate-limit aa sakta hai. Bot khud ruk kar dobara koshish karta hai, aur backup model par bhi chala jata hai.
- **Photos ke license:** Openverse/Pexels/Pixabay ke credits bot khud bhejta hai. Public use se pehle license dekh lein.
- **AI ka code bot ke andar chalta hai** (`/video`, code animation). Isme imports aur khatarnak functions par rok hai, par ye poori tarah safe nahi, isliye **`ALLOWED_IDS` zaroor bharein**.
- **Yaaddasht, notes aur reminders** Upstash ke bina restart par mit jaate hain (section 6).
- **Bot ka token/keys** kisi ko na dikhayein. Galti se dikh jaye to naya token/key banayein.

---

## 9. Dikkat aaye to

| Dikkat | Upay |
|---|---|
| Build me `"/bot.py": not found` | GitHub me file ka naam `bot.py` karein, folder ke andar na ho |
| `InvalidToken` | `/admin` me Telegram token dobara daalein (`123456:AAH...` jaisa). Groq key (`gsk_`) yahan na daalein |
| Bot chup hai | `/admin` ka status dekhein. "Chalu" na ho to token galat hai |
| Hindi text toota dikhe | `/diag` me Hindi font aur Raqm ✅ hone chahiye. `Dockerfile` me `libraqm0`, `fonts-noto-core` hon |
| QR na bane | `/diag` me `segno` ✅ hona chahiye. `requirements.txt` me `segno` ho aur dobara deploy karein |
| Awaaz kam achhi | `edge-tts` fail hone par gTTS chalta hai. `/diag` me `edge-tts ✅` dekhein, Logs me "edge-tts fail" dhundhein |
| Photo nahi milti | `/admin` me Pexels ya Pixabay free key daalein |
| Video bahut der se bane | `/admin` me video size 640x360, FPS 10-15 rakhein |
| Bot kuchh bhool jata hai / settings mit jati hain | Upstash jodein (section 6). `/admin` me Yaaddasht ka status dekhein |
| Settings restart par mit gayi | `RENDER_API_KEY` Environment me daalein (upar section 2) ya Upstash jodein (section 6) |

Logs dekhne ke liye Render → apni service → **Logs**. Keys ya password chhupa kar hi share karein.
