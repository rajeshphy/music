(() => {
  "use strict";

  const title = document.querySelector("#title");
  const subtitle = document.querySelector("#subtitle");
  const updated = document.querySelector("#updated");
  const tabs = document.querySelector("#tabs");
  const filter = document.querySelector("#filter");
  const tracksElement = document.querySelector("#tracks");
  const player = document.querySelector("#player");
  const emptyPlayer = document.querySelector("#empty-player");
  const youtubeSearch = document.querySelector("#youtube-search");

  let tracks = [];
  let searches = [];
  let activeCategory = "all";

  function formatGenerated(value) {
    if (!value) return "Not updated";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Updated";
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      hour: "numeric",
      minute: "2-digit",
      hour12: true
    }).format(date);
  }

  function activeTracks() {
    const text = filter.value.trim().toLowerCase();
    return tracks.filter(track => {
      const inCategory = activeCategory === "all" || (track.categories || []).includes(activeCategory);
      if (!inCategory) return false;
      if (!text) return true;
      return [track.title, track.channel, ...(track.category_labels || []), ...(track.queries || [])]
        .join(" ").toLowerCase().includes(text);
    });
  }

  function tabButton(id, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.category = id;
    button.textContent = label;
    button.classList.toggle("active", id === activeCategory);
    return button;
  }

  function buildTabs() {
    tabs.replaceChildren(tabButton("all", "All"));
    const activeCategories = new Set(tracks.flatMap(track => track.categories || []));
    for (const search of searches) {
      if (activeCategories.has(search.id)) tabs.append(tabButton(search.id, search.label));
    }
    if (activeCategory !== "all" && !activeCategories.has(activeCategory)) activeCategory = "all";
  }

  function play(track) {
    if (!track.embed_url) return;
    player.src = `${track.embed_url}?autoplay=1&rel=0`;
    emptyPlayer.classList.add("hidden");
    youtubeSearch.href = track.url || "https://www.youtube.com/";
  }

  function trackCard(track) {
    const article = document.createElement("article");
    article.className = "track";
    const playButton = document.createElement("button");
    playButton.type = "button";
    playButton.setAttribute("aria-label", `Play ${track.title}`);
    playButton.addEventListener("click", () => play(track));

    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const image = document.createElement("img");
    image.src = track.thumbnail || "";
    image.alt = "";
    image.loading = "lazy";
    thumb.append(image);
    if (track.duration_text) {
      const duration = document.createElement("span");
      duration.className = "duration";
      duration.textContent = track.duration_text;
      thumb.append(duration);
    }
    playButton.append(thumb);

    const body = document.createElement("div");
    body.className = "track-body";
    const heading = document.createElement("h2");
    heading.textContent = track.title;
    const meta = document.createElement("div");
    meta.className = "meta";
    const channel = document.createElement("span");
    channel.textContent = track.channel || "YouTube";
    const open = document.createElement("a");
    open.className = "open";
    open.href = track.url || "#";
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = "Open";
    meta.append(channel, open);

    const chips = document.createElement("div");
    chips.className = "chips";
    if (track.classic) {
      const classic = document.createElement("span");
      classic.className = "chip";
      classic.textContent = "Classic";
      chips.append(classic);
    }
    for (const label of track.category_labels || []) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = label;
      chips.append(chip);
    }
    body.append(heading, meta, chips);
    article.append(playButton, body);
    return article;
  }

  function render() {
    tabs.querySelectorAll("button").forEach(button => {
      button.classList.toggle("active", button.dataset.category === activeCategory);
    });
    const visible = activeTracks();
    tracksElement.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "empty-list";
      empty.textContent = "No tracks found";
      tracksElement.append(empty);
      return;
    }
    visible.forEach(track => tracksElement.append(trackCard(track)));
  }

  async function load() {
    const stamp = Date.now();
    const [configResponse, tracksResponse] = await Promise.all([
      fetch(`data/searches.json?v=${stamp}`, { cache: "no-store" }),
      fetch(`data/tracks.json?v=${stamp}`, { cache: "no-store" })
    ]);
    const [config, data] = await Promise.all([configResponse.json(), tracksResponse.json()]);
    title.textContent = config.portal?.title || "Music Radar";
    subtitle.textContent = config.portal?.subtitle || "";
    updated.textContent = formatGenerated(data.generated_at);
    searches = config.searches || data.searches || [];
    tracks = data.tracks || [];
    buildTabs();
    render();
  }

  tabs.addEventListener("click", event => {
    const button = event.target.closest("button[data-category]");
    if (!button) return;
    activeCategory = button.dataset.category;
    render();
  });
  filter.addEventListener("input", render);
  load().catch(error => {
    console.error(error);
    updated.textContent = "Unavailable";
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "Music data could not be loaded";
    tracksElement.replaceChildren(empty);
  });
})();
