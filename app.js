const CLIENT_ID = 'Ov23lijbCJAjaweAXHyt';
const WORKER_URL = 'https://add-on-auth.ahmedrazaspan.workers.dev';
const REDIRECT_URI = window.location.href.split('#')[0];

// DOM Elements
const loginBtn = document.getElementById('login-btn');
const loginSection = document.getElementById('login-section');
const appSection = document.getElementById('app-section');
const avatar = document.getElementById('avatar');
const username = document.getElementById('username');
const logoutBtn = document.getElementById('logout-btn');
const pushBtn = document.getElementById('push-btn');
const fileInput = document.getElementById('file-input');
const statusLog = document.getElementById('status-log');

// 1. Handle Login
loginBtn.addEventListener('click', () => {
    window.location.href = `https://github.com/login/oauth/authorize?client_id=${CLIENT_ID}&scope=repo&redirect_uri=${REDIRECT_URI}`;
});

// 2. Check for Token on Load
const urlParams = new URLSearchParams(window.location.search);
const code = urlParams.get('code');

if (code) {
    exchangeCodeForToken(code);
} else {
    const token = localStorage.getItem('gh_token');
    if (token) fetchUser(token);
}

async function exchangeCodeForToken(code) {
    try {
        const res = await fetch(WORKER_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        const data = await res.json();
        if (data.access_token) {
            localStorage.setItem('gh_token', data.access_token);
            window.history.replaceState({}, document.title, window.location.pathname);
            fetchUser(data.access_token);
        } else {
            alert("Login failed: " + (data.error_description || data.error));
        }
    } catch (err) {
        alert("Error connecting to auth server.");
    }
}

async function fetchUser(token) {
    const res = await fetch('https://api.github.com/user', {
        headers: { Authorization: `Bearer ${token}` }
    });
    if (res.ok) {
        const user = await res.json();
        avatar.src = user.avatar_url;
        username.textContent = user.login;
        loginSection.classList.add('hidden');
        appSection.classList.remove('hidden');
    } else {
        localStorage.removeItem('gh_token');
    }
}

logoutBtn.addEventListener('click', () => {
    localStorage.removeItem('gh_token');
    window.location.reload();
});

// 3. Handle File Push
pushBtn.addEventListener('click', async () => {
    const token = localStorage.getItem('gh_token');
    const repo = document.getElementById('repo-input').value;
    const branch = document.getElementById('branch-input').value || 'main';
    const files = fileInput.files;

    if (!token) return alert("Please login first.");
    if (!repo || !repo.includes('/')) return alert("Please enter repo as owner/repo");
    if (files.length === 0) return alert("Please select files or a folder.");

    pushBtn.disabled = true;
    pushBtn.textContent = "Pushing...";
    statusLog.innerHTML = "";

    const [owner, repoName] = repo.split('/');

    // Get latest commit SHA for the branch
    const refRes = await fetch(`https://api.github.com/repos/${owner}/${repoName}/git/refs/heads/${branch}`, {
        headers: { Authorization: `Bearer ${token}` }
    });
    
    if (!refRes.ok) {
        alert("Could not find branch. Check repo name and branch.");
        pushBtn.disabled = false;
        pushBtn.textContent = "Push to GitHub";
        return;
    }
    const refData = await refRes.json();
    const latestCommitSha = refData.object.sha;

    // Get the tree of the latest commit
    const commitRes = await fetch(`https://api.github.com/repos/${owner}/${repoName}/git/commits/${latestCommitSha}`, {
        headers: { Authorization: `Bearer ${token}` }
    });
    const commitData = await commitRes.json();
    const baseTreeSha = commitData.tree.sha;

    let tree = [];

    // Upload files (Handle ZIP or Folder)
    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        let path = file.webkitRelativePath || file.name;
        
        // Skip ZIP for now unless we add JSZip (simplified for folders)
        if (file.name.endsWith('.zip')) {
            statusLog.innerHTML += `Skipping ZIP: ${file.name} (Select extracted folder instead)<br>`;
            continue;
        }

        const reader = new FileReader();
        const base64Promise = new Promise((resolve) => {
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.readAsDataURL(file);
        });
        const content = await base64Promise;

        // Create blob
        const blobRes = await fetch(`https://api.github.com/repos/${owner}/${repoName}/git/blobs`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: content, encoding: 'base64' })
        });
        const blobData = await blobRes.json();
        
        tree.push({
            path: path,
            mode: '100644',
            type: 'blob',
            sha: blobData.sha
        });
        
        statusLog.innerHTML += `Uploaded: ${path}<br>`;
        statusLog.scrollTop = statusLog.scrollHeight;
    }

    if (tree.length === 0) {
        alert("No valid files to push.");
        pushBtn.disabled = false;
        pushBtn.textContent = "Push to GitHub";
        return;
    }

    // Create new tree
    const treeRes = await fetch(`https://api.github.com/repos/${owner}/${repoName}/git/trees`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_tree: baseTreeSha, tree: tree })
    });
    const newTreeData = await treeRes.json();

    // Create commit
    const newCommitRes = await fetch(`https://api.github.com/repos/${owner}/${repoName}/git/commits`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
            message: 'Upload via Add-On Web App',
            tree: newTreeData.sha,
            parents: [latestCommitSha]
        })
    });
    const newCommitData = await newCommitRes.json();

    // Update ref
    await fetch(`https://api.github.com/repos/${owner}/${repoName}/git/refs/heads/${branch}`, {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ sha: newCommitData.sha })
    });

    statusLog.innerHTML += `<strong>Success! Commit pushed.</strong>`;
    pushBtn.disabled = false;
    pushBtn.textContent = "Push to GitHub";
});
