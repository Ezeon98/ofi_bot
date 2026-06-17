/**
 * Subscription Frontend
 *
 * Flow:
 * 1. Show 3 plan cards (Free / Pro / Premium)
 * 2. User clicks Pro or Premium → show frequency picker (monthly/annual)
 * 3. User picks frequency → show MercadoPago CardForm
 * 4. CardForm tokenises → send to backend → show result
 */

const API_BASE = new URLSearchParams(window.location.search).get("api") || "";

// ── State ────────────────────────────────────────────────────────────────
let allPlans = [];
let selectedPlan = null;
let selectedTier = null;
let cardFormInstance = null;
let mpInstance = null;

// ── DOM refs ─────────────────────────────────────────────────────────────
const planSection = document.getElementById("plan-selection");
const freqSection = document.getElementById("frequency-section");
const paymentSection = document.getElementById("payment-section");
const resultSection = document.getElementById("result-section");
const resultContent = document.getElementById("result-content");
const selectedPlanInfo = document.getElementById("selected-plan-info");
const submitBtn = document.getElementById("form-checkout__submit");
const formErrors = document.getElementById("form-errors");
const freqCards = document.getElementById("freq-cards");
const freqTitle = document.getElementById("freq-title");

// ── Initialization ───────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    await loadConfig();
    await loadPlans();

    document.querySelectorAll("[data-tier]").forEach((btn) => {
        btn.addEventListener("click", () => showFrequencyPicker(btn.dataset.tier));
    });

    document.getElementById("btn-back-plans").addEventListener("click", () => {
        freqSection.style.display = "none";
        planSection.style.display = "block";
    });
    document.getElementById("btn-back-freq").addEventListener("click", () => {
        paymentSection.style.display = "none";
        freqSection.style.display = "block";
    });
});

async function loadConfig() {
    try {
        const resp = await fetch(API_BASE + "/api/subscriptions/config");
        const data = await resp.json();
        if (!data.public_key) throw new Error("Public key not configured");
        mpInstance = new MercadoPago(data.public_key, { locale: "es-AR" });
    } catch (err) {
        console.error("Failed to load MP config:", err);
    }
}

async function loadPlans() {
    try {
        const resp = await fetch(API_BASE + "/api/subscriptions/plans");
        allPlans = await resp.json();
    } catch (err) {
        console.error("Failed to load plans:", err);
    }

    const proMonthly = allPlans.find((p) => p.tier === "pro" && p.frequency === "monthly");
    const proAnnual = allPlans.find((p) => p.tier === "pro" && p.frequency === "yearly");
    const premMonthly = allPlans.find((p) => p.tier === "premium" && p.frequency === "monthly");
    const premAnnual = allPlans.find((p) => p.tier === "premium" && p.frequency === "yearly");

    const fmt = (v) =>
        new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS", maximumFractionDigits: 0 }).format(v);

    const proEl = document.getElementById("pro-price");
    const proNote = document.getElementById("pro-annual-note");
    if (proMonthly) {
        proEl.innerHTML = `${fmt(proMonthly.price)}<span class="plan-freq">/ mes</span>`;
    }
    if (proAnnual) {
        const saved = proMonthly ? proMonthly.price * 12 - proAnnual.price : 0;
        proNote.textContent = `${fmt(proAnnual.price)} / año · ahorrás ${fmt(saved)}`;
    }

    const premEl = document.getElementById("premium-price");
    const premNote = document.getElementById("premium-annual-note");
    if (premMonthly) {
        premEl.innerHTML = `${fmt(premMonthly.price)}<span class="plan-freq">/ mes</span>`;
    }
    if (premAnnual) {
        const saved = premMonthly ? premMonthly.price * 12 - premAnnual.price : 0;
        premNote.textContent = `${fmt(premAnnual.price)} / año · ahorrás ${fmt(saved)}`;
    }
}

function showFrequencyPicker(tier) {
    selectedTier = tier;
    const tierPlans = allPlans.filter((p) => p.tier === tier);

    if (tierPlans.length === 0) {
        alert("No hay planes disponibles para este tier.");
        return;
    }

    if (tierPlans.length === 1) {
        selectPlan(tierPlans[0]);
        return;
    }

    const label = tier === "premium" ? "Premium" : "Pro";
    freqTitle.textContent = `${label} — Elegí tu frecuencia`;
    freqCards.innerHTML = "";

    const fmt = (v) =>
        new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS", maximumFractionDigits: 0 }).format(v);

    tierPlans.forEach((plan) => {
        const card = document.createElement("div");
        card.className = "freq-card";
        const freqLabel = plan.frequency === "monthly" ? "Mensual" : "Anual";
        const freqSuffix = plan.frequency === "monthly" ? "/mes" : "/año";
        card.innerHTML = `
            <div class="freq-name">${freqLabel}</div>
            <div class="freq-price">${fmt(plan.price)}${freqSuffix}</div>
            ${plan.has_free_trial ? '<div class="freq-trial">✨ Prueba gratis</div>' : ""}
        `;
        card.addEventListener("click", () => selectPlan(plan));
        freqCards.appendChild(card);
    });

    planSection.style.display = "none";
    freqSection.style.display = "block";
    freqSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

function selectPlan(plan) {
    selectedPlan = plan;
    const freqLabel = plan.frequency === "monthly" ? "/mes" : "/año";
    const fmt = (v) =>
        new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS", maximumFractionDigits: 0 }).format(v);

    selectedPlanInfo.innerHTML = `
        <span class="plan-label">${plan.name}</span>
        <span>${fmt(plan.price)} ${freqLabel}</span>
    `;

    freqSection.style.display = "none";
    planSection.style.display = "none";
    paymentSection.style.display = "block";
    submitBtn.disabled = false;

    if (!cardFormInstance && mpInstance) {
        initializeCardForm();
    }
    paymentSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

function initializeCardForm() {
    cardFormInstance = mpInstance.cardForm({
        amount: String(selectedPlan ? selectedPlan.price : "3999"),
        iframe: true,
        form: {
            id: "form-checkout",
            cardNumber: {
                id: "form-checkout__cardNumber",
                placeholder: "4509 9535 6623 3704",
                style: { fontSize: "14px", color: "#FFFFFF", "placeholder-color": "#666666" },
            },
            expirationDate: {
                id: "form-checkout__expirationDate",
                placeholder: "MM/AA",
                style: { fontSize: "14px", color: "#FFFFFF", "placeholder-color": "#666666" },
            },
            securityCode: {
                id: "form-checkout__securityCode",
                placeholder: "123",
                style: { fontSize: "14px", color: "#FFFFFF", "placeholder-color": "#666666" },
            },
            cardholderName: { id: "form-checkout__cardholderName", placeholder: "Como figura en la tarjeta" },
            issuer: { id: "form-checkout__issuer", placeholder: "Banco emisor" },
            installments: { id: "form-checkout__installments", placeholder: "Cuotas" },
            identificationType: { id: "form-checkout__identificationType", placeholder: "Tipo de documento" },
            identificationNumber: { id: "form-checkout__identificationNumber", placeholder: "Número de documento" },
            cardholderEmail: { id: "form-checkout__cardholderEmail", placeholder: "tu@email.com" },
        },
        callbacks: {
            onFormMounted: (error) => {
                if (error) console.warn("CardForm mount error:", error);
                else console.log("CardForm mounted successfully");
            },
            onSubmit: (event) => {
                event.preventDefault();
                handleSubmit();
            },
            onFetching: (resource) => {
                console.log("Fetching:", resource);
                return () => {};
            },
        },
    });
}

async function handleSubmit() {
    if (!selectedPlan) {
        showFormError("Seleccioná un plan primero.");
        return;
    }
    setLoading(true);
    hideFormError();

    try {
        const formData = cardFormInstance.getCardFormData();
        const { token, cardholderEmail } = formData;

        if (!token) {
            showFormError("No se pudo tokenizar la tarjeta. Verificá los datos.");
            setLoading(false);
            return;
        }
        if (!cardholderEmail) {
            showFormError("Ingresá tu email.");
            setLoading(false);
            return;
        }

        const params = new URLSearchParams(window.location.search);
        const uid = params.get("uid") || "0";
        const resp = await fetch(API_BASE + `/api/subscriptions/create?uid=${uid}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                card_token_id: token,
                payer_email: cardholderEmail,
                plan_id: selectedPlan.id,
            }),
        });

        const result = await resp.json();
        if (resp.ok && result.status === "ok") {
            showSuccess(result);
        } else {
            showFormError(result.detail || "Error al crear la suscripción.");
            setLoading(false);
        }
    } catch (err) {
        console.error("Subscription error:", err);
        showFormError("Error de conexión. Intentá de nuevo.");
        setLoading(false);
    }
}

// ── UI Helpers ───────────────────────────────────────────────────────────

function setLoading(loading) {
    submitBtn.disabled = loading;
    submitBtn.querySelector(".btn-text").style.display = loading ? "none" : "inline";
    submitBtn.querySelector(".btn-loading").style.display = loading ? "inline-flex" : "none";
}

function showFormError(message) {
    formErrors.textContent = message;
    formErrors.style.display = "block";
}

function hideFormError() {
    formErrors.style.display = "none";
}

function showSuccess(result) {
    planSection.style.display = "none";
    freqSection.style.display = "none";
    paymentSection.style.display = "none";
    resultSection.style.display = "block";

    const botPhone = new URLSearchParams(window.location.search).get("bot") || "";
    const waLink = botPhone ? `https://wa.me/${botPhone}` : "";
    const chatBtn = waLink
        ? `<a href="${waLink}" target="_blank" rel="noopener" style="display:inline-block;margin-top:16px;padding:12px 24px;background:#25d366;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;">💬 Volver al chat</a>`
        : "";

    resultContent.innerHTML = `
        <div class="result-success">
            <div class="icon">🎉</div>
            <h2>¡Suscripción activada!</h2>
            <p>Tu suscripción a <strong>${selectedPlan ? selectedPlan.name : "Bot"}</strong> fue creada exitosamente.</p>
            <p>Recibirás un email de confirmación.</p>
            <p style="margin-top:16px;color:#ffd700;">Ya podés disfrutar de todas las funciones en WhatsApp 🚀</p>
            ${chatBtn}
            <p id="redirect-msg" style="margin-top:8px;font-size:13px;color:#aaa;">Redirigiendo al chat en <span id="redirect-countdown">5</span>s...</p>
            <p style="margin-top:8px;font-size:12px;color:#666;">ID: ${result.subscription_id || ""}</p>
        </div>
    `;
    resultSection.scrollIntoView({ behavior: "smooth", block: "start" });

    if (waLink) {
        let seconds = 5;
        const countdownEl = document.getElementById("redirect-countdown");
        const interval = setInterval(() => {
            seconds--;
            if (countdownEl) countdownEl.textContent = String(seconds);
            if (seconds <= 0) {
                clearInterval(interval);
                window.location.href = waLink;
            }
        }, 1000);
    }
}
