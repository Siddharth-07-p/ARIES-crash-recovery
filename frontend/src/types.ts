export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  message?: string;
}

export type ConnectionStatus = 'checking' | 'connected' | 'disconnected';
