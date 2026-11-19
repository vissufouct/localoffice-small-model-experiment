export const HOST_API_VERSION = 1;
export type PreviewMode = 'live' | 'background';
export type TaskStatus = 'running' | 'paused' | 'completed' | 'cancelled' | 'failed';
export interface PluginManifest {
  id: string; version: string; hostApi: number;
  capabilities: string[]; permissions: string[];
  dependencies: string[]; trusted: boolean;
}
export interface Annotation {
  id: string; path: string; revision: string;
  scope: 'selection' | 'artifact' | 'page';
  locator: {start?: number; end?: number; page?: number; quote?: string}; comment: string;
}
export interface OperationReceipt {
  id: string; tool: string; version: number;
  status: 'planned' | 'staged' | 'validated' | 'commit_pending' | 'committed' | 'recovery_required';
  path: string; input_hash: string | null; candidate_hash: string;
}
export interface InferenceProvider {
  id: string;
  load(path: string, context: number): Promise<void>;
  unload(): Promise<void>;
  countTokens(messages: {role: string; content: string}[]): Promise<number>;
  stream(messages: {role: string; content: string}[], signal: AbortSignal): AsyncIterable<string>;
}

export interface TaskNetworkPolicy {
  version: 1; online_research_opt_in: boolean;
  research_connector_available: false; effective_network: 'local_only';
  external_ai_allowed: false; project_uploads_allowed: false;
}
