import { redirect } from "next/navigation";

// Campaigns are created in a dialog on the list page.
export default function NewCampaignPage() {
  redirect("/campaigns?add=1");
}
