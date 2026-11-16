import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const HOME = process.env.AGENT_HANDOFF_HOME || "/root/agent-handoff";
const LOG = path.join(HOME, "logs", "integration.log");
const TMP = path.join(HOME, "tmp");

function log(message) {
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true, mode: 0o700 });
    fs.appendFileSync(LOG, `${new Date().toISOString()} opencode-plugin: ${String(message).slice(0, 4000)}\n`, { mode: 0o600 });
  } catch {}
}

function binary() {
  const candidates = [
    process.env.AGENT_HANDOFF_BIN,
    "/usr/local/bin/agent-handoff",
    path.join(HOME, "bin", "agent-handoff"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {}
  }
  return "agent-handoff";
}

function textOfParts(parts) {
  return (parts || [])
    .filter((part) => part && part.type === "text" && !part.synthetic && !part.ignored)
    .map((part) => part.text || "")
    .join("\n")
    .trim();
}

function firstLine(text, limit = 120) {
  const value = (text || "").trim().split(/\r?\n/)[0].trim();
  return (value || "Untitled task").slice(0, limit);
}

function errorString(error) {
  if (!error) return "";
  const name = error.name || error._tag || "error";
  const data = error.data || error;
  let message = "";
  try {
    message = typeof data === "string" ? data : JSON.stringify(data);
  } catch {
    message = String(data);
  }
  return `${name}: ${message}`.slice(0, 2000);
}

function writePayload(payload) {
  fs.mkdirSync(TMP, { recursive: true, mode: 0o700 });
  const file = path.join(TMP, `opencode-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.json`);
  fs.writeFileSync(file, JSON.stringify(payload), { encoding: "utf8", mode: 0o600 });
  return file;
}

export default async function agentHandoff(input) {
  const sessions = new Map();

  function state(sessionID) {
    if (!sessions.has(sessionID)) {
      sessions.set(sessionID, {
        firstPrompt: "",
        lastPrompt: "",
        lastUserMessageID: null,
        model: null,
        agent: null,
        error: "",
        finalizing: false,
        lastFinalizedEvent: null,
      });
    }
    return sessions.get(sessionID);
  }

  async function finalize(sessionID) {
    const st = state(sessionID);
    if (st.finalizing) return;
    st.finalizing = true;
    let payloadFile = null;
    try {
      let session = null;
      try {
        const sessionResult = await input.client.session.get({ path: { id: sessionID }, query: { directory: input.directory } });
        session = sessionResult.data || null;
      } catch (error) {
        log(`session.get failed for ${sessionID}: ${error}`);
      }
      if (session && session.parentID) return;
      let messages = [];
      try {
        const messageResult = await input.client.session.messages({ path: { id: sessionID }, query: { directory: input.directory } });
        messages = messageResult.data || [];
      } catch (error) {
        log(`session.messages failed for ${sessionID}: ${error}`);
        return;
      }
      if (!messages.length) return;
      const users = messages.filter((item) => item.info && item.info.role === "user");
      const assistants = messages.filter((item) => item.info && item.info.role === "assistant");
      if (!users.length) return;
      const lastUser = users[users.length - 1];
      const lastAssistant = assistants.length ? assistants[assistants.length - 1] : null;
      const firstPrompt = (textOfParts(users[0].parts) || st.firstPrompt || "").trim();
      const runPrompt = (textOfParts(lastUser.parts) || st.lastPrompt || "").trim();
      const summary = lastAssistant ? textOfParts(lastAssistant.parts) : "";
      const messageError = (lastAssistant && lastAssistant.info && lastAssistant.info.error) || null;
      const combinedError = errorString(messageError || st.error);
      let outcome = "succeeded";
      if (combinedError) {
        const lower = combinedError.toLowerCase();
        outcome = lower.includes("abort") || lower.includes("cancel") ? "cancelled" : "failed";
      } else if (!summary.trim()) {
        outcome = "blocked";
      }
      const eventKey = lastAssistant && lastAssistant.info && lastAssistant.info.id
        ? `msg:${lastAssistant.info.id}`
        : `usr:${lastUser.info && lastUser.info.id}`;
      if (st.lastFinalizedEvent === eventKey) return;
      const completedAt = lastAssistant && lastAssistant.info && lastAssistant.info.time && lastAssistant.info.time.completed
        ? lastAssistant.info.time.completed
        : Date.now();
      const title = (session && session.title && !/^ses_/i.test(session.title)) ? session.title : firstLine(firstPrompt || runPrompt || summary);
      const model = st.model || (lastAssistant && lastAssistant.info ? `${lastAssistant.info.providerID}/${lastAssistant.info.modelID}` : null);
      const payload = {
        agent: "opencode",
        session_id: sessionID,
        cwd: input.directory,
        title,
        task_prompt: firstPrompt,
        run_prompt: runPrompt,
        summary,
        outcome,
        error: combinedError,
        termination_reason: (lastAssistant && lastAssistant.info && lastAssistant.info.finish) || null,
        model,
        event_key: eventKey,
        completed_at: completedAt,
        tests: { status: "not_verified", details: "OpenCode session captured automatically; no independent test evidence." },
        deployment: { status: "not_verified", details: "OpenCode session captured automatically; no independent deployment evidence." },
      };
      payloadFile = writePayload(payload);
      const result = spawnSync(binary(), ["auto-complete", "--payload-file", payloadFile, "--json"], {
        encoding: "utf8",
        timeout: 15000,
        env: { ...process.env, AGENT_HANDOFF_HOME: HOME },
      });
      if (result.error) {
        log(`auto-complete failed to start: ${result.error.message}`);
      } else if (result.status !== 0) {
        log(`auto-complete exited ${result.status}: ${(result.stderr || "").slice(0, 1000)}`);
      } else {
        st.lastFinalizedEvent = eventKey;
      }
    } catch (error) {
      log(`finalize failed for ${sessionID}: ${error && error.stack ? error.stack : error}`);
    } finally {
      st.finalizing = false;
      if (payloadFile) {
        try {
          fs.unlinkSync(payloadFile);
        } catch {}
      }
    }
  }

  return {
    event: async ({ event }) => {
      try {
        if (!event || !event.properties) return;
        if (event.type === "session.error") {
          const st = state(event.properties.sessionID);
          st.error = errorString(event.properties.error);
          return;
        }
        if (event.type === "message.updated") {
          const info = event.properties.info;
          if (info && info.role === "assistant" && info.error && info.sessionID) {
            const st = state(info.sessionID);
            st.error = errorString(info.error);
          }
          return;
        }
        if (event.type === "session.idle" && event.properties.sessionID) {
          await finalize(event.properties.sessionID);
        }
      } catch (error) {
        log(`event handler failed: ${error && error.stack ? error.stack : error}`);
      }
    },
    "chat.message": async (eventInput, output) => {
      try {
        const st = state(eventInput.sessionID);
        const text = textOfParts(output.parts);
        st.error = "";
        if (!st.firstPrompt) st.firstPrompt = text;
        st.lastPrompt = text;
        st.lastUserMessageID = output.message && output.message.id;
        st.agent = eventInput.agent || st.agent;
        if (eventInput.model) st.model = `${eventInput.model.providerID}/${eventInput.model.modelID}`;
      } catch (error) {
        log(`chat.message failed: ${error && error.stack ? error.stack : error}`);
      }
    },
  };
}
