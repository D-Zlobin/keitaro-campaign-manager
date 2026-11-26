import { CampaignEditorPage } from './pages/CampaignEditorPage'
import { CampaignListPage } from './pages/CampaignListPage'
import { useHashRoute } from './router'

function App() {
  const route = useHashRoute()
  return (
    <main>
      <header className="app-header">
        <h1>
          <a href="#/">Keitaro Campaign Manager</a>
        </h1>
      </header>
      {route.name === 'editor' ? (
        <CampaignEditorPage key={route.campaignId} campaignId={route.campaignId} />
      ) : (
        <CampaignListPage />
      )}
    </main>
  )
}

export default App
