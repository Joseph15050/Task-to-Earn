import os
import json
import uuid
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes
)
from supabase import create_client, Client
import google.generativeai as genai

# --- CONFIGURATION ---

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")

# Multi-fallback key retrieval to prevent startup failure
SUPABASE_KEY = (
    os.environ.get("SERVICE_ROLE") 
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") 
    or os.environ.get("SUPABASE_ANON_KEY")
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

WITHDRAWAL_THRESHOLD_NGN = 5000.00  # Minimum ₦1,000 required to request withdrawal

# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)


# --- DUMMY HTTP SERVER FOR RENDER HEALTH CHECKS ---

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple handler to satisfy Render's port binding checks."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- HELPER FUNCTIONS ---

def get_verified_submissions_count(campaign_id: str) -> int:
    """Returns the count of verified submissions for a specific campaign."""
    res = supabase.table("submissions") \
        .select("id", count="exact") \
        .eq("campaign_id", campaign_id) \
        .eq("status", "VERIFIED") \
        .execute()
    return res.count or 0


# --- COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Save user to DB if not present
    res = supabase.table("users").select("*").eq("telegram_id", user.id).execute()
    if not res.data:
        supabase.table("users").insert({
            "telegram_id": user.id,
            "username": user.username,
            "first_name": user.first_name
        }).execute()

    keyboard = [
        [InlineKeyboardButton("🎯 Active Campaigns", callback_data="list_campaigns")],
        [InlineKeyboardButton("👤 Profile & Balance", callback_data="my_profile")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Welcome {user.first_name}! 👋\nSelect an option below or use the chat menu commands:",
        reply_markup=reply_markup
    )

async def set_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save or update the user's OPay number and account name."""
    user_id = update.effective_user.id
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ **Usage:** `/setaccount <OPAY_NUMBER> <ACCOUNT_NAME>`\n\n"
            "Example: `/setaccount 8012345678 John Doe`\n"
            "📌 *Provide your 10-digit OPay number followed by your full account name.*",
            parse_mode="Markdown"
        )
        return

    account_no = context.args[0].strip()
    account_name = " ".join(context.args[1:]).strip()
    
    # Basic Nigerian phone/account number validation
    clean_acc = account_no.replace("+234", "0").replace(" ", "")
    if clean_acc.startswith("0") and len(clean_acc) == 11:
        clean_acc = clean_acc[1:]

    if not (clean_acc.isdigit() and len(clean_acc) == 10):
        await update.message.reply_text(
            "❌ **Invalid Account Number**\n\n"
            "Please provide a valid 10-digit **OPay Account Number**.",
            parse_mode="Markdown"
        )
        return

    supabase.table("users").update({
        "opay_account_number": clean_acc,
        "opay_account_name": account_name
    }).eq("telegram_id", user_id).execute()

    await update.message.reply_text(
        f"✅ **OPay Account Saved!**\n\n"
        f"• **Bank:** OPay\n"
        f"• **Account Number:** `{clean_acc}`\n"
        f"• **Account Name:** `{account_name}`",
        parse_mode="Markdown"
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View earnings balance, XP, and OPay payout details."""
    user_id = update.effective_user.id
    res = supabase.table("users").select("*").eq("telegram_id", user_id).single().execute()
    u = res.data or {}

    naira_bal = float(u.get("usdt_balance") or 0.00)
    xp = u.get("xp_points", 0)
    opay_acc = u.get("opay_account_number") or "Not Set"
    opay_name = u.get("opay_account_name") or "Not Set"

    msg = (
        f"💳 **Your Financial Summary**\n\n"
        f"• **Naira Balance:** `₦{naira_bal:,.2f}`\n"
        f"• **Accumulated XP:** `{xp} XP`\n"
        f"• **OPay Account Number:** `{opay_acc}`\n"
        f"• **OPay Account Name:** `{opay_name}`\n\n"
        f"💡 *Minimum withdrawal threshold:* `₦{WITHDRAWAL_THRESHOLD_NGN:,.2f}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def campaigns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch and list campaigns via slash command."""
    campaigns = supabase.table("campaigns").select("*").eq("status", "ACTIVE").execute().data
    if not campaigns:
        await update.message.reply_text("No active campaigns available right now. Check back soon!")
        return

    keyboard = []
    for cmp in campaigns:
        verified_cnt = get_verified_submissions_count(cmp['id'])
        max_limit = cmp.get('max_participants')
        
        if max_limit and verified_cnt >= max_limit:
            continue

        spots_label = f" ({verified_cnt}/{max_limit} spots)" if max_limit else ""
        keyboard.append([
            InlineKeyboardButton(f"{cmp['title']} (+₦{cmp.get('reward_usdt', 0)}){spots_label}", callback_data=f"cmp_{cmp['id']}")
        ])

    if not keyboard:
        await update.message.reply_text("All current active campaigns have reached maximum participant limits!")
        return

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Choose a campaign to perform:", reply_markup=reply_markup)

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Queue a user's withdrawal request in the users table for admin review."""
    user = update.effective_user
    user_id = user.id
    
    res = supabase.table("users").select("*").eq("telegram_id", user_id).single().execute()
    u = res.data or {}

    naira_bal = float(u.get("usdt_balance") or 0.00)
    opay_acc = u.get("opay_account_number")
    opay_name = u.get("opay_account_name")
    current_status = u.get("withdrawal_status") or "NONE"

    # Prevent submitting a new request if one is already pending
    if current_status == "PENDING":
        pending_amt = float(u.get("pending_withdrawal_amount") or 0.00)
        await update.message.reply_text(
            f"⏳ **Withdrawal Already Pending**\n\n"
            f"You currently have a pending payout request of `₦{pending_amt:,.2f}`.\n"
            f"Please wait for your previous request to be processed before requesting another.",
            parse_mode="Markdown"
        )
        return

    if not opay_acc or opay_acc == "Not Set" or not opay_name:
        await update.message.reply_text(
            "⚠️ **Missing Payment Details**\n\n"
            "Please set your OPay account number and full name before requesting a withdrawal.\n"
            "Send: `/setaccount <OPAY_NUMBER> <ACCOUNT_NAME>`",
            parse_mode="Markdown"
        )
        return

    if naira_bal < WITHDRAWAL_THRESHOLD_NGN:
        needed = WITHDRAWAL_THRESHOLD_NGN - naira_bal
        await update.message.reply_text(
            f"❌ **Threshold Not Met**\n\n"
            f"• Current Balance: `₦{naira_bal:,.2f}`\n"
            f"• Minimum Required: `₦{WITHDRAWAL_THRESHOLD_NGN:,.2f}`\n"
            f"• You need `₦{needed:,.2f}` more to request a withdrawal.",
            parse_mode="Markdown"
        )
        return

    # Update columns in the users table to flag the pending request
    supabase.table("users").update({
        "usdt_balance": 0.00,
        "pending_withdrawal_amount": naira_bal,
        "withdrawal_status": "PENDING"
    }).eq("telegram_id", user_id).execute()

    await update.message.reply_text(
        f"✅ **Withdrawal Requested!**\n\n"
        f"• **Amount:** `₦{naira_bal:,.2f}`\n"
        f"• **Bank:** OPay\n"
        f"• **Account Number:** `{opay_acc}`\n"
        f"• **Account Name:** `{opay_name}`\n\n"
        f"Your payout request has been registered in the database for batch processing.",
        parse_mode="Markdown"
    )

# --- CALLBACK BUTTON HANDLER ---

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "list_campaigns":
        campaigns = supabase.table("campaigns").select("*").eq("status", "ACTIVE").execute().data
        if not campaigns:
            await query.message.reply_text("No active campaigns right now.")
            return

        keyboard = []
        for cmp in campaigns:
            verified_cnt = get_verified_submissions_count(cmp['id'])
            max_limit = cmp.get('max_participants')
            
            status_text = f" ({verified_cnt}/{max_limit} spots)" if max_limit else ""
            if max_limit and verified_cnt >= max_limit:
                status_text = " [FILLED]"

            keyboard.append([
                InlineKeyboardButton(f"{cmp['title']} (+₦{cmp.get('reward_usdt', 0)}){status_text}", callback_data=f"cmp_{cmp['id']}")
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Choose a campaign to perform:", reply_markup=reply_markup)

    elif query.data.startswith("cmp_"):
        campaign_id = query.data.replace("cmp_", "")
        context.user_data["active_campaign_id"] = campaign_id

        cmp = supabase.table("campaigns").select("*").eq("id", campaign_id).single().execute().data
        verified_cnt = get_verified_submissions_count(campaign_id)
        max_limit = cmp.get('max_participants')

        spots_info = f"{verified_cnt} / {max_limit} completed" if max_limit else "Unlimited"
        is_full = max_limit and (verified_cnt >= max_limit)

        msg = (
            f"📌 **{cmp['title']}**\n\n"
            f"{cmp['description']}\n\n"
            f"💰 **Reward:** ₦{cmp.get('reward_usdt', 0)} Naira | {cmp['xp_reward']} XP\n"
            f"👥 **Spots Taken:** `{spots_info}`\n"
            f"🔗 **Link:** {cmp['action_url']}\n\n"
        )

        if is_full:
            msg += "⚠️ **THIS TASK IS FILLED.** Submissions are closed and will no longer award rewards."
        else:
            msg += "📸 *To complete:* Finish the task, then upload a screenshot reply here."

        await query.message.reply_text(msg, parse_mode="Markdown")

    elif query.data == "my_profile":
        u = supabase.table("users").select("*").eq("telegram_id", query.from_user.id).single().execute().data
        naira_bal = float(u.get("usdt_balance") or 0.00)
        await query.message.reply_text(
            f"👤 **Profile**\n"
            f"• Username: @{u.get('username') or 'N/A'}\n"
            f"• XP Earned: {u.get('xp_points', 0)} XP\n"
            f"• Naira Balance: `₦{naira_bal:,.2f}`\n"
            f"• OPay Number: `{u.get('opay_account_number') or 'Not set'}`\n"
            f"• OPay Name: `{u.get('opay_account_name') or 'Not set'}`",
            parse_mode="Markdown"
        )

# --- IMAGE PROOF PROCESSING ---

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    campaign_id = context.user_data.get("active_campaign_id")
    if not campaign_id:
        await update.message.reply_text("Please tap a campaign from the menu before sending proof.")
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"

    # 1. Fetch Campaign and verify max participant threshold
    cmp = supabase.table("campaigns").select("*").eq("id", campaign_id).single().execute().data
    max_limit = cmp.get("max_participants")
    if max_limit:
        verified_count = get_verified_submissions_count(campaign_id)
        if verified_count >= max_limit:
            await update.message.reply_text(
                f"🛑 **Campaign Limit Reached**\n"
                f"This task has already reached its limit of {max_limit} participants and is no longer accepting proofs.",
                parse_mode="Markdown"
            )
            return

    # 2. Check for existing VERIFIED or PENDING submission
    try:
        existing = (
            supabase.table("submissions")
            .select("*")
            .eq("telegram_id", user_id)
            .eq("campaign_id", campaign_id)
            .in_("status", ["VERIFIED", "PENDING"])
            .execute()
            .data
        )
        if existing:
            await update.message.reply_text("⚠️ You already have an active or approved submission for this campaign.")
            return
    except Exception as e:
        print(f"Error checking duplicates: {e}")

    await update.message.reply_text("⏳ Processing screenshot...")

    try:
        # Download image from Telegram
        photo_file = await update.message.photo[-1].get_file()
        image_bytes = await photo_file.download_as_bytearray()

        # Upload image to Supabase Storage
        file_path = f"proofs/{user_id}_{uuid.uuid4().hex[:8]}.jpg"
        supabase.storage.from_("task-proofs").upload(
            file_path, 
            bytes(image_bytes), 
            file_options={"content-type": "image/jpeg"}
        )
        public_proof_url = supabase.storage.from_("task-proofs").get_public_url(file_path)

        # AI Verification using Gemini
        model = genai.GenerativeModel('gemini-3.5-flash-lite')
        prompt = f"""
Evaluate if this screenshot proves ALL required actions were completed.

Required Actions: {cmp['required_actions']} (e.g., MUST show BOTH Like AND Retweet active).
Target Username: @{username}

Analyze the UI carefully:
1. Is the Like button activated (e.g., red heart)?
2. Is the Retweet/Repost button activated (e.g., green highlight/active state)?

Return JSON strictly in this format:
{{
    "like_verified": boolean,
    "retweet_verified": boolean,
    "is_valid": boolean,
    "confidence": float,
    "reason": "Explain specifically which actions were seen or missing"
}}
"""
        
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(image_bytes)}])
        clean_json_str = response.text.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_json_str)

        # Extract values safely from JSON response
        confidence = float(result.get("confidence", 0.0))
        reason = result.get("reason", "No detailed explanation provided.")
        like_ok = result.get("like_verified", False)
        rt_ok = result.get("retweet_verified", False)
        is_valid = result.get("is_valid", False)

        # Strictly require all verification flags to pass
        if is_valid and like_ok and rt_ok and confidence >= 0.85:
            status = "VERIFIED"
        else:
            status = "REJECTED"

        # Update existing or insert new submission using upsert
        supabase.table("submissions").upsert(
            {
                "telegram_id": user_id,
                "campaign_id": campaign_id,
                "proof_image_url": public_proof_url,
                "status": status,
                "ai_feedback": reason
            },
            on_conflict="telegram_id, campaign_id"
        ).execute()

        # Update User Rewards
        if status == "VERIFIED":
            supabase.rpc("award_user_rewards", {
                "user_id": user_id, 
                "xp_amt": cmp["xp_reward"], 
                "usdt_amt": cmp["reward_usdt"]
            }).execute()
            
            await update.message.reply_text(
                f"✅ **Task Partially Approved!**\n\n"
                f"🎉 Rewards Earned: **+{cmp['xp_reward']} XP** | **+₦{cmp.get('reward_usdt', 0)} Naira**\n"
                f"💡 *Bot Feedback:* {reason}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ **Task Rejected**\n\n"
                f"Reason: {reason}\n"
                f"Please redo the task correctly and upload a clear screenshot.",
                parse_mode="Markdown"
            )

    except Exception as err:
        print("ERROR IN HANDLE_SCREENSHOT:", traceback.format_exc())
        await update.message.reply_text(
            f"⚠️ Verification error: `{str(err)}`\nPlease re-upload your screenshot.",
            parse_mode="Markdown"
        )
        

# --- MAIN ENTRYPOINT ---

if __name__ == "__main__":
    # Start background HTTP server thread for Render health checks
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register Command Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setaccount", set_account))
    app.add_handler(CommandHandler("setwallet", set_account)) # Alias for old command users
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("campaigns", campaigns_command))
    app.add_handler(CommandHandler("withdraw", withdraw))

    # Register Callback & Media Handlers
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))

    print("Bot is live with OPay Naira payments and updated schema!")
    app.run_polling()
