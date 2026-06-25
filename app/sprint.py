"""Sprint Orchestrator — the generation loop for the Sprint model.

Sprint is a conductor model (Qwen3.6-35B-A3B) that acts as an orchestrator.
It can:
  1. Answer directly when it knows the answer
  2. Emit «expert:name» blocks to consult specialized knowledge sources
  3. After drafting, run a self-check pass
  4. If low confidence, emit «escalate» to request guidance from a stronger model

The orchestrator intercepts these blocks mid-generation, calls the
appropriate service, injects results, and resumes generation.

Block format:
  «expert:ds9»
  Natural language question for the expert.
  «/expert»

  «escalate»
  What Sprint is unsure about.
  «/escalate»

  «proceed»  (self-check passed, deliver the response)
"""
import json
import logging
import re
import time

import requests

from app.experts import call_expert, get_expert_descriptions

logger = logging.getLogger('ecolyxis.sprint')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sprint system prompt — kept simple for a 0.8B model
SPRINT_SYSTEM_PROMPT = """\
You are Sprint, the fast assistant for Ecolyxis. You answer users directly and quickly.

You have specialised knowledge experts available. You MUST consult the relevant expert BEFORE answering any question that involves specialist topics. Do NOT rely on your own knowledge for specialist questions — your training data may be inaccurate or outdated. Instead, ask the expert first, then use their answer.

To consult an expert, write:

«expert:EXPERT_NAME»
Your natural language question here.
«/expert»

Wait for the expert's answer, then continue your response using their information.

{expert_descriptions}

For general conversation (greetings, opinions, simple help) you can answer directly without an expert.

After finishing your response, review it for accuracy. If you are confident, end with «proceed» on its own line. If you are unsure about something specific, request guidance:

«escalate»
Describe what you are unsure about.
«/escalate»

Rules:
- ALWAYS consult an expert for factual questions about their domain
- Never fabricate facts — ask an expert if unsure
- Keep responses concise and friendly
- You can consult the same expert multiple times with different questions
"""

# Stop sequences for the LLM — we pause generation when these appear
STOP_SEQUENCES = ["«/expert»", "«/escalate»", "«proceed»"]

# Regex to parse expert blocks from generated text
EXPERT_BLOCK_RE = re.compile(
    r'«expert:(\w+)»\s*\n(.*?)\n?\s*$',
    re.DOTALL,
)
ESCALATE_BLOCK_RE = re.compile(
    r'«escalate»\s*\n(.*?)\n?\s*$',
    re.DOTALL,
)

# Max iterations in the generation loop (prevent infinite loops)
MAX_ITERATIONS = 10


# ---------------------------------------------------------------------------
# Sprint Client
# ---------------------------------------------------------------------------

class SprintClient:
    """LLM client configured for the Sprint model.

    Handles gpu-manager variant switching: before generating, it requests
    the appropriate variant on the manager API and waits for the switch.
    """

    # Class-level cache: which variant is currently loaded on the GPU
    _current_variant = None

    def __init__(self, base_url, model, max_tokens=2048, variant="sprint",
                 manager_url=None):
        self.base_url = base_url
        self.model = model
        self.max_tokens = max_tokens
        self.variant = variant
        # Derive manager URL from proxy URL (same host, port 8090)
        if manager_url:
            self.manager_url = manager_url
        else:
            # base_url is like http://192.168.122.5:8081/v1
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            host = parsed.hostname  # 192.168.122.5 (no port)
            scheme = parsed.scheme
            self.manager_url = f"{scheme}://{host}:8090"

    def _ensure_variant(self):
        """Tell the gpu-manager to load our variant, wait until ready."""
        if SprintClient._current_variant == self.variant:
            return  # already loaded
        try:
            # Request the switch
            resp = requests.post(
                f"{self.manager_url}/switch",
                json={"variant": self.variant},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Switched to variant '%s'", self.variant)

            # Poll until ready (not switching) — 35B model can take 2+ min to load
            for _ in range(150):  # max 150 seconds
                try:
                    sr = requests.get(f"{self.manager_url}/status", timeout=5)
                    st = sr.json()
                    if st.get("ready") and not st.get("switching"):
                        if st.get("variant") == self.variant:
                            SprintClient._current_variant = self.variant
                            return
                        # Wrong variant loaded — retry switch
                        break
                except Exception:
                    pass
                time.sleep(1)
            logger.warning("Variant switch to '%s' may not have completed", self.variant)
        except Exception as e:
            logger.warning("Variant switch failed: %s — proceeding anyway", e)

    def generate(self, messages, stop=None):
        """Non-streaming generation. Returns the full text."""
        self._ensure_variant()
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.7,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if stop:
            payload["stop"] = stop

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def generate_stream(self, messages, stop=None):
        """Streaming generation. Yields (text_delta, token_count) tuples.

        The token_count tracks cumulative tokens for progress display.
        """
        self._ensure_variant()
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.7,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if stop:
            payload["stop"] = stop

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()

        token_count = 0
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                usage = chunk.get("usage")
                if usage:
                    token_count = usage.get("completion_tokens", token_count)
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content") or ""
                if content:
                    token_count += 1  # approximate
                    yield content, token_count
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SprintOrchestrator:
    """Manages the Sprint generation loop: generate → intercept blocks →
    call experts/escalate → resume → self-check → deliver.
    """

    def __init__(self, client, escalation_client=None):
        """
        Args:
            client: SprintClient for the Sprint model
            escalation_client: SprintClient for the stronger model (Standard).
                               If None, escalation is disabled.
        """
        self.client = client
        self.escalation_client = escalation_client

    def generate(self, conversation_messages, on_progress=None, on_event=None):
        """Run the full Sprint generation loop.

        Args:
            conversation_messages: List of {"role": ..., "content": ...} dicts
                representing the conversation history (system prompt is added
                automatically by the orchestrator).
            on_progress: Callback(token_count: int) for live token streaming.
            on_event: Callback(event: dict) for lifecycle events:
                {"type": "expert_start", "expert": "ds9", "question": "..."}
                {"type": "expert_done", "expert": "ds9", "confidence": 0.85}
                {"type": "escalate_start"}
                {"type": "escalate_done", "guidance": "..."}
                {"type": "self_check_start"}
                {"type": "self_check_done", "passed": True/False}
                {"type": "content", "text": "..."}  # content chunk for display

        Returns:
            dict with keys:
                response: final response text (clean, markers stripped)
                expert_calls: list of {expert, question, answer, confidence}
                escalated: bool
                escalation_guidance: str or None
                self_check_passed: bool
                iterations: int
                total_tokens: int
        """
        expert_descriptions = get_expert_descriptions()
        system_prompt = SPRINT_SYSTEM_PROMPT.format(
            expert_descriptions=expert_descriptions
        )

        # Build the full message list for Sprint
        messages = [{"role": "system", "content": system_prompt}] + conversation_messages

        accumulated = ""
        expert_calls = []
        escalated = False
        escalation_guidance = None
        self_check_passed = False
        total_tokens = 0

        for iteration in range(MAX_ITERATIONS):
            # Generate with stop sequences
            iteration_text = ""
            for text_delta, token_count in self.client.generate_stream(
                messages, stop=STOP_SEQUENCES
            ):
                iteration_text += text_delta
                total_tokens = token_count
                if on_progress:
                    on_progress(total_tokens)

            accumulated += iteration_text

            # Emit content for display
            if on_event:
                on_event({"type": "content", "text": iteration_text})

            # Determine what stop sequence was hit (or if generation finished)
            # The stop sequence is included in the generated text by llama-server
            hit_expert = "«/expert»" in iteration_text or self._has_open_expert(iteration_text)
            hit_escalate = "«/escalate»" in iteration_text or self._has_open_escalate(iteration_text)
            hit_proceed = "«proceed»" in iteration_text
            generation_finished = not (hit_expert or hit_escalate or hit_proceed)

            if hit_expert:
                # Parse and handle expert block
                expert_name, question, clean_text = self._parse_expert_block(iteration_text)
                if expert_name and question:
                    # Call the expert
                    if on_event:
                        on_event({"type": "expert_start", "expert": expert_name, "question": question})

                    try:
                        result = call_expert(expert_name, question)
                        answer = result["answer"]
                        confidence = result["confidence"]

                        expert_calls.append({
                            "expert": expert_name,
                            "question": question,
                            "answer": answer,
                            "confidence": confidence,
                            "sources": result.get("sources", []),
                        })

                        if on_event:
                            on_event({
                                "type": "expert_done",
                                "expert": expert_name,
                                "confidence": confidence,
                                "answer": answer,
                            })

                        # Inject expert response into context
                        expert_response = (
                            f"\n«expert_result»\n{answer}\n«/expert_result»\n\n"
                            f"Now continue your response using this information."
                        )
                        messages.append({"role": "assistant", "content": iteration_text})
                        messages.append({"role": "user", "content": expert_response})

                    except Exception as e:
                        logger.error("Expert '%s' call failed: %s", expert_name, e)
                        error_msg = f"\n[Expert {expert_name} is unavailable: {e}]\n\nAnswer based on your own knowledge or ask the user."
                        messages.append({"role": "assistant", "content": iteration_text})
                        messages.append({"role": "user", "content": error_msg})

                    continue  # resume generation

                # Malformed expert block — treat as end of generation
                generation_finished = True

            if hit_escalate:
                # Parse and handle escalation
                concern, clean_text = self._parse_escalate_block(iteration_text)
                if concern and self.escalation_client:
                    if on_event:
                        on_event({"type": "escalate_start", "concern": concern})

                    # Call the stronger model for guidance
                    guidance = self._request_escalation(concern, conversation_messages)

                    if guidance:
                        escalated = True
                        escalation_guidance = guidance

                        if on_event:
                            on_event({"type": "escalate_done", "guidance": guidance})

                        # Inject guidance as a named entity
                        guidance_msg = (
                            f"\n«guidance from Standard»\n{guidance}\n«/guidance»\n\n"
                            f"Rephrase this guidance in your own words and incorporate it "
                            f"into your response. Then finish with «proceed»."
                        )
                        messages.append({"role": "assistant", "content": iteration_text})
                        messages.append({"role": "user", "content": guidance_msg})
                        continue  # resume generation
                    else:
                        # Escalation failed — tell Sprint to answer its best
                        messages.append({"role": "assistant", "content": iteration_text})
                        messages.append({"role": "user", "content": "Standard is unavailable. Give your best answer and end with «proceed»."})
                        continue

                elif concern and not self.escalation_client:
                    # No escalation client configured — proceed anyway
                    logger.warning("Sprint wanted to escalate but no escalation client configured")
                    messages.append({"role": "assistant", "content": iteration_text})
                    messages.append({"role": "user", "content": "Escalation is unavailable. Give your best answer and end with «proceed»."})
                    continue

                generation_finished = True

            if hit_proceed or generation_finished:
                # Self-check phase
                if on_event:
                    on_event({"type": "self_check_start"})

                # If we hit «proceed», Sprint already self-checked and passed
                if hit_proceed:
                    self_check_passed = True
                else:
                    # Generation finished without «proceed» — run explicit self-check
                    self_check_passed = self._run_self_check(
                        messages, accumulated, on_event
                    )

                break

        # Clean up the accumulated text
        response = self._clean_output(accumulated)

        return {
            "response": response,
            "expert_calls": expert_calls,
            "escalated": escalated,
            "escalation_guidance": escalation_guidance,
            "self_check_passed": self_check_passed,
            "iterations": len(expert_calls) + (1 if escalated else 0) + 1,
            "total_tokens": total_tokens,
        }

    def _has_open_expert(self, text):
        """Check if text contains an opening «expert: tag without a closing tag."""
        opens = len(re.findall(r'«expert:\w+»', text))
        closes = text.count("«/expert»")
        return opens > closes

    def _has_open_escalate(self, text):
        """Check if text contains an opening «escalate» without a closing tag."""
        opens = text.count("«escalate»")
        closes = text.count("«/escalate»")
        return opens > closes

    def _parse_expert_block(self, text):
        """Extract expert name and question from generated text.

        Returns (expert_name, question, clean_text).
        """
        # Find the last expert block
        match = re.search(
            r'«expert:(\w+)»\s*\n(.*?)(?:«/expert»|$)',
            text,
            re.DOTALL,
        )
        if match:
            expert_name = match.group(1)
            question = match.group(2).strip()
            return expert_name, question, text

        return None, None, text

    def _parse_escalate_block(self, text):
        """Extract escalation concern from generated text.

        Returns (concern, clean_text).
        """
        match = re.search(
            r'«escalate»\s*\n(.*?)(?:«/escalate»|$)',
            text,
            re.DOTALL,
        )
        if match:
            concern = match.group(1).strip()
            return concern, text

        return None, text

    def _request_escalation(self, concern, conversation_messages):
        """Call the stronger model for guidance.

        The stronger model sees Sprint's concern and the conversation context,
        and provides guidance that Sprint will rephrase.
        """
        if not self.escalation_client:
            return None

        escalation_client = self.escalation_client
        escalation_prompt = (
            "You are providing guidance to a smaller assistant model (Sprint). "
            "Sprint has drafted a response but is unsure about something. "
            "Provide clear, accurate guidance that Sprint can use to improve its answer. "
            "Be concise and direct. Do not address the user — address Sprint.\n\n"
            f"Sprint's concern: {concern}"
        )

        # Include the last user message for context
        last_user = ""
        for msg in reversed(conversation_messages):
            if msg["role"] == "user":
                last_user = msg["content"][:500]
                break

        messages = [
            {"role": "system", "content": escalation_prompt},
            {"role": "user", "content": f"The user asked: {last_user}\n\nProvide guidance."},
        ]

        try:
            guidance = self.escalation_client.generate(messages)
            return guidance.strip()
        except Exception as e:
            logger.error("Escalation call failed: %s", e)
            return None

    def _run_self_check(self, messages, draft, on_event=None):
        """Run an explicit self-check on the draft.

        Returns True if Sprint confirms the answer is good.
        """
        check_prompt = (
            "Review your response above for accuracy. "
            "If everything is correct, respond with ONLY «proceed».\n"
            "If you are unsure about something specific, respond with:\n"
            "«escalate»\nYour concern\n«/escalate»"
        )

        messages_copy = list(messages)
        messages_copy.append({"role": "assistant", "content": draft})
        messages_copy.append({"role": "user", "content": check_prompt})

        try:
            result = self.client.generate(messages_copy, stop=["«/escalate»", "«proceed»"])

            if "«proceed»" in result:
                if on_event:
                    on_event({"type": "self_check_done", "passed": True})
                return True
            elif "«escalate»" in result:
                if on_event:
                    on_event({"type": "self_check_done", "passed": False})
                # Could handle escalation here, but for now just proceed
                logger.info("Sprint self-check flagged low confidence but proceeding")
                return False
            else:
                # No marker — assume pass
                return True
        except Exception as e:
            logger.warning("Self-check failed: %s — assuming pass", e)
            return True

    def _clean_output(self, text):
        """Remove Sprint control markers from the final output."""
        # Remove complete expert blocks (with closing tag)
        text = re.sub(r'«expert:\w+».*?«/expert»', '', text, flags=re.DOTALL)
        # Remove incomplete expert blocks — the question Sprint asked the expert.
        # These appear when the stop sequence «/expert» is stripped by the API.
        # Match from «expert:name» to end of the question (next sentence/paragraph).
        text = re.sub(r'«expert:\w+»\s*\n.*?(?=\n\n|\n[A-Z]|\Z)', '', text, flags=re.DOTALL)
        # Remove any remaining bare expert tags
        text = re.sub(r'«expert:\w+»', '', text)
        text = re.sub(r'«/expert»', '', text)
        # Remove expert result injection markers (keep content)
        text = re.sub(r'«expert_result».*?«/expert_result»', '', text, flags=re.DOTALL)
        text = re.sub(r'«expert_result»', '', text)
        text = re.sub(r'«/expert_result»', '', text)
        # Remove escalation blocks
        text = re.sub(r'«escalate».*?«/escalate»', '', text, flags=re.DOTALL)
        text = re.sub(r'«escalate».*?(?=\n\n|\n[A-Z]|\Z)', '', text, flags=re.DOTALL)
        text = re.sub(r'«escalate»', '', text)
        text = re.sub(r'«/escalate»', '', text)
        # Remove guidance blocks (keep content, remove markers)
        text = re.sub(r'«guidance from \w+»', '', text)
        text = re.sub(r'«/guidance»', '', text)
        # Remove proceed markers and instruction text
        text = text.replace('«proceed»', '')
        # Remove instruction lines that the orchestrator injected
        text = re.sub(r'Now continue your response.*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
        text = re.sub(r'Rephrase this guidance.*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
        text = re.sub(r'Standard is unavailable.*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
        text = re.sub(r'Escalation is unavailable.*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
        text = re.sub(r'Review your response.*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
        text = re.sub(r'Give your best answer.*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
        # Clean up extra whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
