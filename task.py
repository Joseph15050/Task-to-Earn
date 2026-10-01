import os
import json
import uuid
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
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


# Initialize Clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)

# --- COMMANDS ---

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
        f"Welcome {user.first_name}! 👋\nSelect an option below to get started:",
        reply_markup=reply_markup
    )

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
            keyboard.append([InlineKeyboardButton(f"{cmp['title']} (+${cmp['reward_usdt']})", callback_data=f"cmp_{cmp['id']}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Choose a campaign to perform:", reply_markup=reply_markup)

    elif query.data.startswith("cmp_"):
        campaign_id = query.data.replace("cmp_", "")
        context.user_data["active_campaign_id"] = campaign_id

        cmp = supabase.table("campaigns").select("*").eq("id", campaign_id).single().execute().data
        
        msg = (
            f"📌 **{cmp['title']}**\n\n"
            f"{cmp['description']}\n\n"
            f"💰 **Reward:** ${cmp['reward_usdt']} USDT | {cmp['xp_reward']} XP\n"
            f"🔗 **Link:** {cmp['action_url']}\n\n"
            f"📸 *To complete:* Finish the task, then upload a screenshot reply here."
        )
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif query.data == "my_profile":
        u = supabase.table("users").select("*").eq("telegram_id", query.from_user.id).single().execute().data
        await query.message.reply_text(
            f"👤 **Profile**\n"
            f"• Username: @{u['username']}\n"
            f"• XP Earned: {u['xp_points']} XP\n"
            f"• Wallet: `{u.get('usdt_address') or 'Not set'}`",
            parse_mode="Markdown"
        )

# --- IMAGE PROOF PROCESSING ---

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    campaign_id = context.user_data.get("active_campaign_id")
    if not campaign_id:
        await update.message.reply_text("Please tap a campaign from the menu before sending proof.")
        return

    # Check duplicate submission
    existing = supabase.table("submissions").select("*").eq("telegram_id", update.effective_user.id).eq("campaign_id", campaign_id).execute().data
    if existing:
        await update.message.reply_text("⚠️ You have already submitted proof for this campaign.")
        return

    await update.message.reply_text("⏳ Processing screenshot & evaluating with AI...")

    # 1. Download image from Telegram
    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()

    # 2. Upload image to Supabase Storage ('task-proofs')
    file_path = f"proofs/{update.effective_user.id}_{uuid.uuid4().hex[:8]}.jpg"
    supabase.storage.from_("task-proofs").upload(file_path, bytes(image_bytes), {"content-type": "image/jpeg"})
    public_proof_url = supabase.storage.from_("task-proofs").get_public_url(file_path)

    # 3. AI Verification using Gemini
    cmp = supabase.table("campaigns").select("*").eq("id", campaign_id).single().execute().data
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""
    Evaluate if this screenshot proves social media action completion.
    Required Actions: {cmp['required_actions']}
    Target Username: @{update.effective_user.username}

    Return JSON strictly with format:
    {{
        "is_valid": boolean,
        "confidence": float,
        "reason": "short explanation"
    }}
    """

    try:
        response = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": bytes(image_bytes)}])
        result = json.loads(response.text.replace("```json", "").replace("```", "").strip())

        status = "VERIFIED" if (result["is_valid"] and result["confidence"] >= 0.85) else "REJECTED"

        # Record submission
        supabase.table("submissions").insert({
            "telegram_id": update.effective_user.id,
            "campaign_id": campaign_id,
            "proof_image_url": public_proof_url,
            "status": status,
            "ai_feedback": result["reason"]
        }).execute()

        if status == "VERIFIED":
            supabase.rpc("increment_xp", {"user_id": update.effective_user.id, "amount": cmp["xp_reward"]}).execute()
            await update.message.reply_text(f"✅ **Approved!** +{cmp['xp_reward']} XP added.")
        else:
            await update.message.reply_text(f"❌ **Rejected:** {result['reason']}")

    except Exception as e:
        await update.message.reply_text("Failed to verify image format. Please try re-sending a clear screenshot.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_screenshot))
    
    print("Bot is live!")
    app.run_polling()
