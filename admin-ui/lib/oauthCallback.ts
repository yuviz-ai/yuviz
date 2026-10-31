// The one provider error the console acts on is access_denied (the user
// pressed Cancel). Every other provider or server error text stays hidden and
// gets the generic message.
export const FAILURE_MESSAGE = "We could not complete the connection. Go back to Connected Apps and try again.";
export const DECLINED_MESSAGE = "The connection was declined, so nothing was connected. Go back to Connected Apps and try again if you want to connect.";

export function callbackFailureMessage(providerError: string | null): string {
  return providerError === "access_denied" ? DECLINED_MESSAGE : FAILURE_MESSAGE;
}
