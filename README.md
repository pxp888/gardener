# 🌿 Gardener

**Turn-based context compaction extension for [pi](https://github.com/earendil-works/pi)**

Gardener is a context management extension for the `pi` coding agent (`@earendil-works/pi-coding-agent`). Instead of letting conversation context balloon until hitting token thresholds or context overflow, Gardener continuously prunes older history into a compact, factual state note while keeping the most recent turns untouched.

The goal is to match 'human like' information discarding.  When a person is looking through a file for something and does not find it, they dismiss the file and simply remember their target wasn't there.  An LLM will have that file permanently stuck in the context window.  

This avoids that by running a parallel agent that constantly prunes the context to remove boilerplate, summarize conclusions and failures, while preserving essential information for the task.  



---

## 💡 Why Gardener?

In long coding sessions, conversation context quickly accumulates noise:
* Multi-page tool outputs and logs.
* Failed search attempts and discarded exploration paths.
* Boilerplate code that can easily be regenerated.

Default agent compactions usually wait until a hard token cliff (e.g. 100k+ tokens or overflow recovery), triggering a massive truncation pass. 

**Gardener takes a continuous approach:**
* **Rolling window:** Keeps the last $K$ turns (default 5) completely untouched.
* **Factual extraction:** As older turns age out of the protected window, they are summarized into high-signal conclusions (e.g. *"checked records.db — only contains expense summaries"* or *"added health check endpoint to server.py"*).
* **Iterative merging:** Each pass only summarizes newly aged-out turns and merges them into the previous state note, keeping compaction latency and token usage bounded ($O(1)$ per pass).
* **Consistent model alignment:** The gardener pass uses the session's active model, ensuring the summarizer follows the same reasoning style and capability as the main agent.
* **Unified `/compact` handling:** Intercepts manual `/compact` commands, context threshold warnings, and overflow recovery to apply the same structured gardener routine.

This should allow logical continuation of long running tasks without exhausting context limits, and in theory should improve reasoning as the model should never into a state of excessive context.  


---

## 🔍 How It Works

```
[ Previous State Summary ] + [ Newly Aged-Out Turns ]
                  │
                  ▼
          (Gardener Model Call)
                  │
                  ▼
[ New State Note ] + [ Protected Tail (Last K Turns Verbatim) ]
```

1. **Turn counting:** Pi counts turns at assistant response boundaries (`turn_end`).
2. **Planning:** At each boundary after every $N$ turns, Gardener checks if turns have aged out past the protected tail ($K$).
3. **Compaction Draft:** The aged-out turns and previous summary are sent to the model with preservation guidelines. The resulting summary is committed to Pi as a canonical `compaction` boundary entry.
4. **Context Refresh:** Pi replays the compaction summary followed by the kept entries for subsequent agent turns.

---

## Downsides 

Because this alters context on a regular basis the prefill cache will be discarded regularly.  With an online provider this will significantly increase prefill costs.  If you're running locally, this is less of an issue, but the fact that summarizing is constantly happening will take additional time.  

Normal compaction routines take time as well, but they only run on demand. This runs much more often, takes more tokens, but should also do a better job of preserving information.  


---

## example test

_This includes results from [memento](https://github.com/pxp888/memento), another context summarization method._



Task (`goal.md`):

> develop a fastapi todo list app that is publicly available and editable.
> Users should be able to create and destroy lists.
> This should be able to display multiple lists, and items should be draggable between lists.
> this should run in its own venv, and be served on port 8000.

Model: `thinkfarm/qwen3.8:27b-ud-q4_k_m` — 10/11/2026.

|                                    | memento              | gardener          | normal     |
| ---------------------------------- | --------------------:| -----------------:| ----------:|
| Assistant turns                    | 13                   | 10                | 16         |
| Tool results / calls               | 15 / 15              | 10 / 10           | 17 / 17    |
| Compactions                        | 5 (≈1 per 2.6 turns) | 2 (1 per 5 turns) | 0          |
| ↑ input tokens                     | 27k                  | 33k               | 10k        |
| ↓ output tokens                    | 14k                  | 17k               | 18k        |
| R cache-read tokens                | 157k                 | 137k              | 309k       |
| **Total tokens (↑+↓+R)**           | **≈198k**            | **≈187k**         | **≈337k**  |
| Avg context per turn ((↑+R)/turns) | ≈14.2k               | 17.0k             | ≈19.9k     |
| **Est. cost**                      | **$0.065**           | **$0.076**        | **$0.078** |

*Cost = ↑×$0.45/M + ↓×$3.20/M + R×$0.05/M (assumed prices).*

On this task both gated variants beat normal on total tokens, and per-turn context stays flat instead of growing with each step.   



-----------


## 🚀 Installation

### Project-level (automatic discovery)
Place the extension in your project's `.pi` configuration directory:
```bash
mkdir -p .pi/extensions/gardener
cp index.ts .pi/extensions/gardener/index.ts
```
*(Note: Your project workspace must be trusted by pi).*

### Global extension
To enable Gardener across all sessions:
```bash
mkdir -p ~/.pi/agent/extensions/gardener
cp index.ts ~/.pi/agent/extensions/gardener/index.ts
```

---

## ⚙️ Configuration

Tune Gardener via environment variables:

| Variable | Default | Description |
| :--- | :---: | :--- |
| `PI_GARDENER_TURNS` | `1` | Garden every $N$ completed turns. Set to `0` to disable automatic periodic gardening (manual `/compact` will still use Gardener). |
| `PI_GARDENER_PROTECTED_TURNS` | `5` | Number of recent assistant turns to keep verbatim. |

### Tuning Tips

* **API Providers & Prompt Caching:**  
  If you are using providers with prefix caching (Claude, OpenAI, Gemini), running every turn (`PI_GARDENER_TURNS=1`) constantly alters the prompt prefix. Setting `PI_GARDENER_TURNS=3` or `5` preserves prompt cache hits across intermediate turns.
* **Local Models & `llama.cpp`:**  
  For local models (e.g. `qwen3.8:27b`), setting `PI_GARDENER_TURNS=3` avoids waiting for local generation after every single step.

---

## 📝 Customizing the Prompt

All persona instructions, preservation criteria, and operational rules are centralized in `GARDENER_PROMPT` inside [`.pi/extensions/gardener/index.ts`](.pi/extensions/gardener/index.ts).

You can modify preservation priorities or adjust operational rules (e.g. formatting constraints, thinking/reasoning guidelines) directly in that single location.

---


