/**
 * Capture-first driver (ticket #103).
 *
 * Drives a real Jellyfin client — the official `@jellyfin/sdk`, the exact
 * network layer used by Jellyfin Web/desktop and Switchfin — through the
 * base scenarios a PS4 user runs: server discovery → login → open
 * library → list items → item detail → playback attempt → poster.
 *
 * The backend records every request to <CS_UK_JF_CAPTURE_DIR>/capture.jsonl
 * via its request-middleware hook. This script exists to REPRODUCE that
 * capture; the frozen fixture file is what tests replay.
 *
 * Usage:
 *   1. Start the backend with CS_UK_JF_CAPTURE_DIR=/tmp/jf-capture-staging.
 *   2. `npm install` in this directory (first time).
 *   3. Set JF_BASE_URL to the backend URL (default http://127.0.0.1:8000).
 *   4. `npm run capture`.
 */
import { Jellyfin } from "@jellyfin/sdk";
import {
  getImageApi,
  getItemsApi,
  getMediaInfoApi,
  getPlaystateApi,
  getSystemApi,
  getSessionApi,
  getUserApi,
  getUserViewsApi,
  getUserLibraryApi,
  getVideosApi,
} from "@jellyfin/sdk/lib/utils/api/index.js";

const BASE_URL = process.env.JF_BASE_URL || "http://127.0.0.1:8000";

const client = new Jellyfin({
	clientInfo: { name: "SwitchfinLike", version: "1.0.0" },
	deviceInfo: { name: "Capture PS4", id: "capture-ps4-dev" },
});

const api = client.createApi(BASE_URL);

let userId = null;
let accessToken = null;

/** Run a step, log its HTTP settlement, continue on expected errors. */
async function run(label, fn) {
	try {
		const r = await fn();
		console.log(`[ok]   ${label}`);
		return r;
	} catch (e) {
		const resp = e?.response;
		console.log(
			`[err]  ${label} -> ${resp ? `${resp.status} ${resp.statusText}` : e.message}`,
		);
		return null;
	}
}

async function main() {
	// 1. Server discovery — the "add server" screen.
	const sys = getSystemApi(api);
	await run("GET /System/Info/Public", () => sys.getPublicSystemInfo());

	// 2. Login: accept-any-credentials handshake.
	await run("POST /Users/AuthenticateByName", async () => {
		const result = await getUserApi(api).authenticateUserByName({
			authenticateUserByName: { Username: "ps4user", Pw: "anything" },
		});
		userId = result.data?.User?.Id;
		accessToken = result.data?.AccessToken;
		api.accessToken = accessToken;
	});

	// 3. Open library: Views list for the signed-in user.
	const views = await run("GET /Users/{id}/Views", () =>
		userId ? getUserViewsApi(api).getUserViews({ userId }) : Promise.reject(new Error("no user")),
	);
	const firstViewId = views?.data?.Items?.[0]?.Id;

	// 4. List items in the first view: the home-row cards.
	const library = await run("GET /Items (library)", () =>
		firstViewId && userId ? getItemsApi(api).getItems({ userId, parentId: firstViewId }) : Promise.reject(new Error("no view")),
	);
	const firstItemId = library?.data?.Items?.[0]?.Id;

	// 5. Item detail: the first listed card (movie/series), plus its
	//    season children when the card is a series. The all-zeros id is
	//    the cold-cache fallback (unknown key -> 404, never 5xx).
	await run("GET /Items/{id} (detail, listed)", () =>
		firstItemId && userId ? getUserLibraryApi(api).getItem({ userId, itemId: firstItemId }) : Promise.reject(new Error("no item")),
	);
	let firstSeasonId = null;
	const listedType = library?.data?.Items?.[0]?.Type;
	if (firstItemId && listedType === "Series") {
		const seasons = await run("GET /Items?parentId=series (seasons)", () =>
			userId ? getItemsApi(api).getItems({ userId, parentId: firstItemId }) : Promise.reject(new Error("no user")),
		);
		firstSeasonId = seasons?.data?.Items?.[0]?.Id;
	}
	await run("GET /Items/{id} (detail, unknown)", () =>
		userId ? getUserLibraryApi(api).getItem({ userId, itemId: "00000000000000000000000000000000" }) : Promise.reject(new Error("no user")),
	);

	// 6. Playback attempt: PlaybackInfo -> stream -> sessions (no-op surface).
	await run("POST /PlaybackInfo", () =>
		getMediaInfoApi(api).getPostedPlaybackInfo({
			customPlaybackInfo: { ...defaultPlaybackInfo() },
			itemId: "00000000000000000000000000000000",
		}),
	);
	await run("GET /Videos/{id}/stream", () =>
		getVideosApi(api).getVideoStream({ itemId: "00000000000000000000000000000000" }),
	);
	const sessions = getPlaystateApi(api);
	await run("POST /Sessions/Playing", () =>
		sessions.reportPlaybackStart({
			reportPlaybackStartRequest: { ItemId: "00000000000000000000000000000000", MediaSourceId: "0", IsPaused: false, CanSeek: false, PositionTicks: 0, PlayMethod: "DirectStream" },
		}),
	);

	// 7. Poster route: the listed card's art when available.
	await run("GET /Items/{id}/Images/Primary", () =>
		firstItemId ? getImageApi(api).getItemImage({ itemId: firstItemId, imageType: "Primary" }) : getImageApi(api).getItemImage({ itemId: "00000000000000000000000000000000", imageType: "Primary" }),
	);

	// Logout: report session ended (native no-op semantics).
	await run("Logout /Sessions/Logout", () => getSessionApi(api).reportSessionEnded());
}

function defaultPlaybackInfo() {
	return {
		UserId: userId,
		MaxStreamingBitrate: 10000000,
		StartTimeTicks: 0,
		AutoOpenLiveStream: true,
		MediaSourceId: "00000000000000000000000000000000",
		EnableDirectPlay: true,
		EnableDirectStream: true,
		AllowVideoStreamCopy: true,
		AllowAudioStreamCopy: true,
	};
}

main().catch((e) => {
	console.error("fatal:", e);
	process.exit(1);
});