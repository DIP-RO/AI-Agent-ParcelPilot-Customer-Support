export interface Persona {
  persona_id: string;
  display_name: string;
  org: string;
  kind: "customer" | "staff";
  account_id?: string;
  role?: string;
  blurb: string;
  suggested_prompts: string[];
}

export interface Session {
  token: string;
  persona: Persona;
  scope: string;
}

export interface ToolEvent {
  id?: string;
  name: string;
  input?: unknown;
  result?: unknown;
  is_error?: boolean;
  status: "running" | "done";
}

export interface PendingAction {
  action_type: string;
  label: string;
  params: Record<string, unknown>;
  requested_by: string;
  signed_payload: string;
  approval_note?: string;
  // Client-assigned unique id: signed_payload is deterministic, so two identical
  // prepared actions would otherwise share (and cross-update) card state.
  client_id?: string;
  ui_status?: "pending" | "executed" | "declined" | "failed";
  result_summary?: string;
}

export type Entry =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; tools: ToolEvent[]; actions: PendingAction[]; streaming: boolean }
  | { kind: "note"; text: string }
  | { kind: "error"; text: string };

export type StreamEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_start"; name: string }
  | { type: "tool_call"; id: string; name: string; input: unknown }
  | { type: "tool_result"; id: string; name: string; is_error: boolean; result: unknown }
  | { type: "pending_action"; action: PendingAction }
  | { type: "turn_done"; history: unknown[]; stop_reason: string }
  | { type: "error"; message: string };
