# VisitAI — AI Document Generation for Italian Commercial Agents

SaaS platform that generates visit reports, follow-up emails, and commercial offers from informal voice descriptions in under 30 seconds. Built for Italy's ~200,000 *agenti di commercio*.

## Stack
- **Backend:** FastAPI + SQLAlchemy async + SQLite/PostgreSQL
- **AI:** Claude Haiku via Anthropic SDK
- **Payments:** Stripe (checkout, portal, webhooks)
- **Auth:** JWT cookie-based + bcrypt
- **Deploy:** Railway via Dockerfile

## Pricing
| Plan | Price | Limit |
|------|-------|-------|
| Free | €0 | 10 docs/month |
| Pro | €39/month | Unlimited |
| Team | €89/month | 5 users |

## Features
- Natural language → professional document in <30s
- Visit report + email follow-up + commercial offer
- Dark UI optimized for mobile field use
- Stripe subscription management

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env  # add ANTHROPIC_API_KEY + Stripe keys
uvicorn main:app --reload
```

## Deploy
```bash
railway up
```

---
*Zero competitors in this niche. Built in 3 days.*
