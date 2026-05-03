const walletMessage = document.querySelector("#wallet-message");
const walletAddress = document.querySelector("#wallet-address");
const connectButton = document.querySelector("#connect-wallet");
const installButton = document.querySelector("#phantom-install");
const contractButtons = document.querySelectorAll(".contract-button");
const contractMessage = document.querySelector("#contract-message");

function shortAddress(publicKey) {
  const value = publicKey.toString();
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

function setContractButtonsConnected() {
  const labels = [
    "Start Manifesto + Community contract",
    "Check Community NFT first",
    "Check Community NFT first",
  ];

  contractButtons.forEach((button, index) => {
    button.textContent = labels[index];
    button.disabled = index !== 0;
    button.title =
      index === 0
        ? "Contract call will be enabled after deployment details are wired."
        : "This contract is available after the Community NFT exists in the wallet.";
  });
}

function explainPendingContract(event) {
  const contractName = event.currentTarget.dataset.contract;
  const names = {
    member: "Manifesto + Community",
    licence: "Symbiotic Licence",
    pioneer: "Resonant Pioneer",
  };

  contractMessage.hidden = false;
  contractMessage.textContent = `${names[contractName]} contract selected. The wallet connection is ready; the on-chain call will be enabled after the deployed program ID, RPC endpoint, and contract accounts are wired into this page.`;
}

function updateWalletState() {
  const provider = window.solana;

  if (!provider?.isPhantom) {
    walletMessage.textContent =
      "Phantom was not detected in this browser. Install Phantom, then return to sign in.";
    connectButton.disabled = true;
    installButton.hidden = false;
    return;
  }

  installButton.hidden = true;
  connectButton.disabled = false;
  walletMessage.textContent = "Phantom is ready. Sign in to begin.";

  if (provider.publicKey) {
    walletMessage.textContent = "Wallet connected. You can begin the DAO steps.";
    walletAddress.hidden = false;
    walletAddress.textContent = `Connected wallet: ${shortAddress(provider.publicKey)}`;
    connectButton.textContent = "Wallet connected";
    setContractButtonsConnected();
  }
}

async function connectWallet() {
  const provider = window.solana;

  if (!provider?.isPhantom) {
    updateWalletState();
    return;
  }

  try {
    const response = await provider.connect();
    walletMessage.textContent = "Wallet connected. You can begin the DAO steps.";
    walletAddress.hidden = false;
    walletAddress.textContent = `Connected wallet: ${shortAddress(response.publicKey)}`;
    connectButton.textContent = "Wallet connected";
    setContractButtonsConnected();
  } catch (error) {
    walletMessage.textContent =
      error?.message || "Wallet connection was cancelled. Try again when ready.";
  }
}

connectButton?.addEventListener("click", connectWallet);
contractButtons.forEach((button) =>
  button.addEventListener("click", explainPendingContract),
);
window.addEventListener("load", updateWalletState);
