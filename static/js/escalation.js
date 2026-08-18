(function () {
  function normalize(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/).filter((word) => word.length > 2).filter((word) => !["the", "and", "for", "this", "that", "with", "from", "what", "how", "can", "you"].includes(word));
  }
  function similarity(left, right) {
    const a = new Set(normalize(left)); const b = new Set(normalize(right));
    if (!a.size || !b.size) return 0;
    return [...a].filter((word) => b.has(word)).length / Math.max(a.size, b.size);
  }
  function attach({ form, mode }) {
    const state = { previousQuestion: "", unresolved: 0, dismissed: false, card: null };
    const difficultyPattern = /\b(explain simpler|simpler|easier|confus|don't understand|do not understand|still stuck|again|another way|more detail|step by step|why)\b/i;
    const weakAnswerPattern = /(could not answer|couldn't answer|not enough information|does not contain|doesn't contain|no ai provider|check the .*configuration|i am not sure|i'm not sure)/i;
    function removeCard() { state.card?.remove(); state.card = null; }
    function showCard(level) {
      if (state.dismissed || state.card) return;
      const strong = level === "strong"; const card = document.createElement("aside");
      card.className = `escalation-card ${strong ? "is-strong" : ""}`; card.setAttribute("role", "status");
      card.innerHTML = `<div><span class="escalation-label">${strong ? "A DIFFERENT WAY TO LEARN" : "NEED ANOTHER EXPLANATION?"}</span><strong>${strong ? "Let the voice teacher work through this with you." : "You can ask the voice teacher to explain this aloud."}</strong><p>${strong ? "You have been circling this topic for a while. Speaking naturally can make the next explanation easier to follow." : "Try a spoken explanation, examples, and follow-up questions without typing."}</p></div><div class="escalation-actions"><a class="escalation-primary" href="/voice?reason=stuck&mode=${encodeURIComponent(mode)}">Start voice teacher ↗</a><button class="escalation-dismiss" type="button">Keep typing</button></div>`;
      card.querySelector(".escalation-dismiss").onclick = () => { state.dismissed = true; removeCard(); };
      form.insertAdjacentElement("afterend", card); state.card = card;
    }
    return { record(question, answer) { const repeated = state.previousQuestion && similarity(question, state.previousQuestion) >= 0.62; const difficult = difficultyPattern.test(question) || repeated || weakAnswerPattern.test(answer); state.unresolved = difficult ? state.unresolved + 1 : 0; state.previousQuestion = question; if (state.unresolved >= 4) showCard("strong"); else if (state.unresolved >= 3) showCard("gentle"); if (!state.unresolved) removeCard(); } };
  }
  window.StudyBuddyEscalation = { attach };
  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("chat-form");
    const input = document.getElementById("chat-input");
    if (!form || !input) return;
    const mode = form.classList.contains("rag-form") ? "rag" : form.classList.contains("mode-chat-form") ? "general" : "dashboard";
    const escalation = attach({ form, mode });
    let pendingQuestion = "";
    form.addEventListener("submit", () => { pendingQuestion = input.value.trim(); }, true);
    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
      const response = await originalFetch(...args);
      const url = typeof args[0] === "string" ? args[0] : args[0]?.url || "";
      if (pendingQuestion && (url.includes("/api/chat") || url.includes("/api/rag/chat"))) {
        response.clone().json().then((data) => {
          const last = [...(data.messages || [])].reverse().find((item) => item.role === "assistant");
          escalation.record(pendingQuestion, last?.message || "");
          pendingQuestion = "";
        }).catch(() => {});
      }
      return response;
    };
  });
})();
