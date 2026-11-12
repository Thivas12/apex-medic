# Browser walkthrough

Use the [hosted demo](https://apex-medic.onrender.com/ui/) or a local instance.
All values below are synthetic. This exercises the case workflow, not a real
connection test.

## Create a case

Choose **Synthetic demo**, select **Ethernet** and enter:

> Synthetic demo: players teleport during an Apex match.

Create the case and save its ID. Select **Packet-loss readings** when asked for
the symptom, then save the answer. The next step should request measurements.

## Record readings and a comparison

Enter these readings, with “Synthetic affected-match baseline; wired connection;
same game mode” as the conditions:

| Reading | Baseline | Comparison / verification |
| --- | --- | --- |
| Latency | 55 ms | 52 ms |
| Packet loss | 8% | 2% |
| FPS | 144 | 144 |

After saving the baseline, check that the next step is `compare_data_centers`.
It should ask for both data-centre names and readings while keeping game mode
and connection unchanged. Supporting guidance should include the source link.

With a provider configured, click **Explain this step**. Its text should begin
with the exact action, optionally followed by a reviewed card. If the provider
is unavailable, the original action should remain visible.

Record the comparison with these values:

- **Conditions:** Synthetic Demo A versus Demo B; same wired connection and game mode.
- **Outcome:** Improved.
- **Notes:** Synthetic baseline 55 ms / 8% loss / 144 FPS; comparison 52 ms / 2% loss / 144 FPS. Some symptoms remain; cause unknown.

## Verify and review

The next step should be `verify_improvement`. Add the verification readings
from the table, labelled “Synthetic verification under comparable conditions.”
Save an improved test result, recording the original baseline and verification
values in the notes.

Check that the next action is `review_verification`, the case is still
investigating and no cause has been confirmed. **Explain this step** should now
return reviewed text without calling the model.

## Check saved state and access

1. Inspect history for the answer, observations and test results.
2. Refresh and reopen the ID in the same browser. The evidence should remain.
3. Try the ID in a private browser session. It should return not found.
4. When running locally, restart Uvicorn and reopen the case in the original
   browser to check persistence.
5. Use **Manage or delete a saved case**, review the preview and confirm the
   exact ID. After deletion, reopening the case should fail.

Session access lasts 30 days from creation. Clearing the cookie loses access;
there is no recovery. Expiry does not delete stored data, so remove demo cases
when finished.
