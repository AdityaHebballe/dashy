        const API_URL = '/api/dashboard';
        const CONTROL_URL = '/api/control';

        let currentTrackId = '';
        let currentLyricsKey = '';
        let parsedLyrics = [];
        let hasSyncedLyrics = false;
        let activeLyricIndex = -1;
        let isTransitioning = false;
        let playbackIsPlaying = false;
        let currentDuration = 0;
        let localTime = 0;
        let lastUpdateTime = Date.now();
        let playbackTickTimeout = null;
        let dashboardPollTimeout = null;
        let dashboardFetchInFlight = false;
        let lastProgressPaintTime = 0;
        let lyricScrollAnimationId = null;
        let lyricScrollTarget = 0;

        const STATS_POLL_INTERVAL_MS = 250;
        const MUSIC_POLL_INTERVAL_MS = 2500;
        const PLAYBACK_TICK_MS = 120;
        const IDLE_TICK_MS = 500;
        const PROGRESS_PAINT_INTERVAL_MS = 120;
        const LYRIC_SCROLL_DURATION_MS = 240;

        const progressFill = document.getElementById('progress-fill');
        const trackNameElem = document.getElementById('track-name');
        const artistNameElem = document.getElementById('artist-name');
        const albumArtElem = document.getElementById('album-art');
        const blurElem = document.getElementById('bg-blur');
        const cpuStatElem = document.getElementById('cpu-stat');
        const gpuStatElem = document.getElementById('gpu-stat');
        const ramStatElem = document.getElementById('ram-stat');
        const ramDetailElem = document.getElementById('ram-detail');
        const diskStatElem = document.getElementById('disk-stat');
        const cpuGaugeElem = document.getElementById('cpu-gauge');
        const gpuGaugeElem = document.getElementById('gpu-gauge');
        const ramFillElem = document.getElementById('ram-fill');
        const diskBarElem = document.getElementById('disk-bar');

        function setGaugeValue(element, percent) {
            const circumference = 220;
            const normalized = Math.max(0, Math.min(100, percent));
            element.style.strokeDashoffset = `${circumference - ((normalized / 100) * circumference)}`;
        }

        function startInterpolation() {
            if (playbackTickTimeout) clearTimeout(playbackTickTimeout);

            const tick = () => {
                const now = performance.now();
                if (playbackIsPlaying && currentDuration > 0) {
                    const currentTimestamp = Date.now();
                    const elapsed = (currentTimestamp - lastUpdateTime) / 1000;
                    localTime += elapsed;
                    lastUpdateTime = currentTimestamp;

                    const clampTime = Math.min(localTime, currentDuration);
                    syncLyrics(clampTime + 0.25);

                    if ((now - lastProgressPaintTime) >= PROGRESS_PAINT_INTERVAL_MS) {
                        const progressPct = (clampTime / currentDuration) * 100;
                        progressFill.style.transform = `scaleX(${Math.max(0, Math.min(1, progressPct / 100))})`;
                        lastProgressPaintTime = now;
                    }
                } else {
                    lastUpdateTime = Date.now();
                }

                playbackTickTimeout = setTimeout(tick, playbackIsPlaying ? PLAYBACK_TICK_MS : IDLE_TICK_MS);
            };

            tick();
        }

        const musicView = document.getElementById('music-view');
        const statsView = document.getElementById('stats-view');
        const lyricsContainer = document.getElementById('lyrics-container');
        const playPauseBtn = document.getElementById('playpause-btn');
        const prevBtn = document.getElementById('prev-btn');
        const nextBtn = document.getElementById('next-btn');

        function formatTime(seconds) {
            if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
            const mins = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        }

        function formatTemp(value) {
            return Number.isFinite(value) ? `${value.toFixed(1)}°C` : '--';
        }

        function getTempClass(value) {
            if (!Number.isFinite(value)) return 'temp-none';
            if (value < 55) return 'temp-cool';
            if (value < 75) return 'temp-warm';
            if (value < 88) return 'temp-hot';
            return 'temp-crit';
        }

        function updateTempBox(idBox, idStat, value) {
            const box = document.getElementById(idBox);
            const stat = document.getElementById(idStat);
            box.className = 'stats-temp-right ' + getTempClass(value);
            if (Number.isFinite(value)) {
                stat.innerHTML = `<svg class="temp-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"></path></svg> ${value.toFixed(1)}°C`;
            } else {
                stat.textContent = '--';
            }
        }

        function clampPercent(value) {
            const numeric = Number(value) || 0;
            return Math.max(0, Math.min(100, numeric));
        }

        let currentArtworkColors = '';

        function setTheme(artwork) {
            if (!artwork) return;
            const newColors = `${artwork.bgColor}|${artwork.textColor1}`;
            if (currentArtworkColors === newColors) return;
            currentArtworkColors = newColors;

            document.documentElement.style.setProperty('--bg-color', `#${artwork.bgColor || '070b10'}`);
            document.documentElement.style.setProperty('--text-1', `#${artwork.textColor1 || 'f5f7fb'}`);
            document.documentElement.style.setProperty('--text-2', `#${artwork.textColor2 || 'b8c4d2'}`);
            document.documentElement.style.setProperty('--text-3', `#${artwork.textColor4 || artwork.textColor3 || '7a8794'}`);
            document.documentElement.style.setProperty('--panel-color', `#${artwork.bgColor || '070b10'}a8`);
            document.documentElement.style.setProperty('--glow', `#${artwork.textColor1 || 'ffffff'}33`);
        }

        function showView(mode) {
            const showMusic = mode === 'music';
            musicView.classList.toggle('hidden', !showMusic);
            statsView.classList.toggle('hidden', showMusic);
            document.body.classList.toggle('stats-active', !showMusic);
        }

        function renderLyrics(lyricsText, isSynced) {
            lyricsContainer.innerHTML = '';
            parsedLyrics = [];
            hasSyncedLyrics = Boolean(isSynced);
            activeLyricIndex = -1;

            if (!lyricsText) {
                const emptyLine = document.createElement('div');
                emptyLine.className = 'lyric-line active unsynced';
                emptyLine.textContent = 'No lyrics found for this track';
                lyricsContainer.appendChild(emptyLine);
                return;
            }

            if (hasSyncedLyrics) {
                const regex = /\[(\d{2}):(\d{2}(?:\.\d{2,3})?)\](.*)/;
                const fragment = document.createDocumentFragment();
                lyricsText.split('\n').forEach((rawLine) => {
                    const match = rawLine.match(regex);
                    if (!match) return;

                    const minutes = Number.parseInt(match[1], 10);
                    const seconds = Number.parseFloat(match[2]);
                    const text = match[3].trim();
                    if (!text) return;

                    const element = document.createElement('div');
                    element.className = 'lyric-line';
                    element.textContent = text;
                    fragment.appendChild(element);

                    parsedLyrics.push({
                        time: (minutes * 60) + seconds,
                        element
                    });
                });
                lyricsContainer.appendChild(fragment);
            } else {
                const fragment = document.createDocumentFragment();
                lyricsText
                    .split('\n')
                    .map((line) => line.trim())
                    .filter(Boolean)
                    .forEach((line, index) => {
                        const element = document.createElement('div');
                        element.className = `lyric-line unsynced${index === 0 ? ' active' : ''}`;
                        element.textContent = line;
                        fragment.appendChild(element);
                    });
                lyricsContainer.appendChild(fragment);
            }
        }

        function applyLyricState(activeIndex) {
            const previousIndex = activeLyricIndex;
            if (previousIndex === activeIndex) return;

            if (previousIndex >= 0) {
                const previousLine = parsedLyrics[previousIndex]?.element;
                if (previousLine) {
                    previousLine.classList.remove('active');
                    if (previousIndex < activeIndex) {
                        previousLine.classList.add('previous');
                    } else {
                        previousLine.classList.remove('previous');
                    }
                }
            }

            if (activeIndex < 0) {
                return;
            }

            if (previousIndex > activeIndex) {
                for (let i = activeIndex + 1; i < previousIndex; i += 1) {
                    parsedLyrics[i]?.element.classList.remove('previous', 'active');
                }
            } else {
                for (let i = Math.max(previousIndex + 1, 0); i < activeIndex; i += 1) {
                    parsedLyrics[i]?.element.classList.add('previous');
                    parsedLyrics[i]?.element.classList.remove('active');
                }
            }

            const activeLine = parsedLyrics[activeIndex]?.element;
            if (activeLine) {
                activeLine.classList.remove('previous');
                activeLine.classList.add('active');
            }
        }

        function scrollLyricsToActive(activeIndex) {
            const activeLine = parsedLyrics[activeIndex];
            if (!activeLine) return;

            const targetTop = Math.max(
                0,
                activeLine.element.offsetTop - (lyricsContainer.clientHeight / 2) + (activeLine.element.clientHeight / 2)
            );
            const startTop = lyricsContainer.scrollTop;
            const distance = targetTop - startTop;

            if (Math.abs(distance) < 2) {
                lyricsContainer.scrollTop = targetTop;
                lyricScrollTarget = targetTop;
                return;
            }

            if (lyricScrollAnimationId) {
                cancelAnimationFrame(lyricScrollAnimationId);
                lyricScrollAnimationId = null;
            }

            lyricScrollTarget = targetTop;
            const startTime = performance.now();

            const step = (now) => {
                const elapsed = now - startTime;
                const progress = Math.min(1, elapsed / LYRIC_SCROLL_DURATION_MS);
                const eased = 1 - Math.pow(1 - progress, 3);
                lyricsContainer.scrollTop = startTop + (distance * eased);

                if (progress < 1) {
                    lyricScrollAnimationId = requestAnimationFrame(step);
                } else {
                    lyricsContainer.scrollTop = lyricScrollTarget;
                    lyricScrollAnimationId = null;
                }
            };

            lyricScrollAnimationId = requestAnimationFrame(step);
        }

        function syncLyrics(currentTime) {
            if (!hasSyncedLyrics || parsedLyrics.length === 0) return;

            let nextActiveIndex = activeLyricIndex;

            if (nextActiveIndex < 0 || currentTime < parsedLyrics[nextActiveIndex].time) {
                nextActiveIndex = -1;
                for (let i = 0; i < parsedLyrics.length; i += 1) {
                    if (currentTime >= parsedLyrics[i].time) {
                        nextActiveIndex = i;
                    } else {
                        break;
                    }
                }
            } else {
                for (let i = nextActiveIndex + 1; i < parsedLyrics.length; i += 1) {
                    if (currentTime >= parsedLyrics[i].time) {
                        nextActiveIndex = i;
                    } else {
                        break;
                    }
                }
            }

            if (nextActiveIndex === -1 || nextActiveIndex === activeLyricIndex) return;

            applyLyricState(nextActiveIndex);
            activeLyricIndex = nextActiveIndex;
            scrollLyricsToActive(nextActiveIndex);
        }

        function updateStatsUI(data) {
            const cpu = clampPercent(data.cpu);
            const gpu = clampPercent(data.gpu);
            const ram = clampPercent(data.ram);
            const disk = Number(data.disk) || 0;
            const ramUsedGb = Number(data.ram_used_gb) || 0;
            const ramTotalGb = Number(data.ram_total_gb) || 0;

            cpuStatElem.textContent = `${Math.round(cpu)}%`;
            gpuStatElem.textContent = `${Math.round(gpu)}%`;
            ramStatElem.textContent = `${Math.round(ram)}%`;
            ramDetailElem.textContent = ramTotalGb > 0
                ? `${ramUsedGb.toFixed(1)} / ${ramTotalGb.toFixed(1)} GB used`
                : '--';
            diskStatElem.textContent = `${disk.toFixed(1)} MB/s`;

            const diskPct = Math.min(100, (disk / 500) * 100);
            
            setGaugeValue(cpuGaugeElem, cpu);
            setGaugeValue(gpuGaugeElem, gpu);
            ramFillElem.style.height = `${ram}%`;
            diskBarElem.style.width = `${diskPct}%`;

            updateTempBox('cpu-temp-box', 'cpu-temp-stat', data.cpu_temp);
            updateTempBox('gpu-temp-box', 'gpu-temp-stat', data.gpu_temp);
        }

        function updatePlayPauseButton(isPlaying) {
            playbackIsPlaying = Boolean(isPlaying);
            playPauseBtn.textContent = playbackIsPlaying ? '❚❚' : '▶';
            playPauseBtn.setAttribute('aria-label', playbackIsPlaying ? 'Pause playback' : 'Resume playback');
        }

        function updateMusicUI(data) {
            showView('music');
            setTheme(data.artwork);

            const displayTrack = data.track || 'Unknown track';
            const displayArtist = data.artist || 'Unknown artist';
            
            if (trackNameElem.textContent !== displayTrack) {
                trackNameElem.textContent = displayTrack;
            }
            if (artistNameElem.textContent !== displayArtist) {
                artistNameElem.textContent = displayArtist;
            }
            
            let rawUrl = data.artwork?.url || (typeof data.artwork === 'string' ? data.artwork : '');
            if (rawUrl) {
                rawUrl = rawUrl.replace(/\/(?:\{w\}x\{h\}|\d+x\d+)[a-zA-Z]*\.(png|jpg|jpeg)$/i, '/800x800bb.jpg');
                rawUrl = rawUrl.replace('{w}', '800').replace('{h}', '800');
            }
            
            const placeholderSvg = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LDAuMSkiIHN0cm9rZS13aWR0aD0iMSI+PHBhdGggZD0iTTkgMThWNWwxMi0ydjEzIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz48Y2lyY2xlIGN4PSI2IiBjeT0iMTgiIHI9IjMiLz48Y2lyY2xlIGN4PSIxOCIgY3k9IjE2IiByPSIzIi8+PC9zdmc+';
            const targetSrc = rawUrl || placeholderSvg;
            
            if (albumArtElem.getAttribute('src') !== targetSrc) {
                albumArtElem.style.padding = rawUrl ? '0' : '15%';
                albumArtElem.setAttribute('src', targetSrc);
            }
            
            const targetBg = rawUrl ? `url("${rawUrl}")` : 'none';
            if (!blurElem.style.backgroundImage.includes(rawUrl) || rawUrl === '') {
                blurElem.style.backgroundImage = targetBg;
            }

            currentDuration = (data.duration || 0) / 1000;
            localTime = data.current_time || 0;
            lastUpdateTime = Date.now();
            lastProgressPaintTime = 0;
            playbackIsPlaying = Boolean(data.is_playing);

            progressFill.style.transform = currentDuration > 0
                ? `scaleX(${Math.max(0, Math.min(1, localTime / currentDuration))})`
                : 'scaleX(0)';
            updatePlayPauseButton(playbackIsPlaying);

            const nextTrackId = `${data.track || ''}::${data.artist || ''}::${data.duration || ''}`;
            const nextLyricsKey = `${data.lyrics_synced ? 'synced' : 'plain'}::${data.lyrics || ''}`;
            if (nextTrackId !== currentTrackId) {
                currentTrackId = nextTrackId;
                currentLyricsKey = nextLyricsKey;
                renderLyrics(data.lyrics, data.lyrics_synced);
                if (lyricScrollAnimationId) {
                    cancelAnimationFrame(lyricScrollAnimationId);
                    lyricScrollAnimationId = null;
                }
                lyricsContainer.scrollTop = 0;
                lyricScrollTarget = 0;
                if (hasSyncedLyrics && parsedLyrics.length > 0) {
                    syncLyrics(localTime + 0.25);
                }
            } else if (nextLyricsKey !== currentLyricsKey) {
                currentLyricsKey = nextLyricsKey;
                renderLyrics(data.lyrics, data.lyrics_synced);
                if (lyricScrollAnimationId) {
                    cancelAnimationFrame(lyricScrollAnimationId);
                    lyricScrollAnimationId = null;
                }
                lyricsContainer.scrollTop = 0;
                lyricScrollTarget = 0;
                if (hasSyncedLyrics && parsedLyrics.length > 0) {
                    syncLyrics(localTime + 0.25);
                }
            }
        }

        async function sendControl(action) {
            if (isTransitioning) return;
            isTransitioning = true;

            try {
                await fetch(`${CONTROL_URL}/${action}`, { method: 'POST' });
                if (dashboardPollTimeout) {
                    clearTimeout(dashboardPollTimeout);
                    dashboardPollTimeout = null;
                }
                setTimeout(fetchDashboard, 180);
            } catch (error) {
                console.error('Control request failed', error);
            } finally {
                setTimeout(() => {
                    isTransitioning = false;
                }, 220);
            }
        }

        async function fetchDashboard() {
            if (dashboardFetchInFlight) return;
            dashboardFetchInFlight = true;

            try {
                const response = await fetch(API_URL);
                const data = await response.json();

                if (data.mode === 'music' && data.has_active_track) {
                    updateMusicUI(data);
                } else {
                    updateStatsUI(data);
                    showView('stats');
                }
            } catch (error) {
                console.error('Dashboard offline or unreachable', error);
            } finally {
                dashboardFetchInFlight = false;
                const nextPollDelay = musicView.classList.contains('hidden')
                    ? STATS_POLL_INTERVAL_MS
                    : MUSIC_POLL_INTERVAL_MS;
                dashboardPollTimeout = setTimeout(fetchDashboard, nextPollDelay);
            }
        }

        prevBtn.addEventListener('click', () => sendControl('previous'));
        nextBtn.addEventListener('click', () => sendControl('next'));
        playPauseBtn.addEventListener('click', () => sendControl('playpause'));

        startInterpolation();
        fetchDashboard();
    