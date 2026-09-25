/** Shape returned by POST /api/token and consumed by the client. */
export type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantToken: string;
  participantName: string;
};
