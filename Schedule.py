from nba_api.stats.endpoints import leaguegamefinder
import pandas as pd


def get_nba_matchups(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Get NBA matchups between two dates.

    Args:
        start_date: Start date in MM/DD/YYYY format
        end_date: End date in MM/DD/YYYY format

    Returns:
        DataFrame with columns: GAME_DATE, HOME_TEAM, AWAY_TEAM
    """
    games = leaguegamefinder.LeagueGameFinder(
        date_from_nullable=start_date,
        date_to_nullable=end_date
    )

    df = games.get_data_frames()[0]

    home = df[df['MATCHUP'].str.contains('vs.', regex=False)][['GAME_ID', 'GAME_DATE', 'TEAM_NAME']].copy()
    away = df[df['MATCHUP'].str.contains('@', regex=False)][['GAME_ID', 'TEAM_NAME']].copy()

    merged = home.merge(away, on='GAME_ID', suffixes=('_HOME', '_AWAY'))

    result = merged[['GAME_DATE', 'TEAM_NAME_HOME', 'TEAM_NAME_AWAY']].copy()
    result.columns = ['GAME_DATE', 'HOME_TEAM', 'AWAY_TEAM']
    result = result.sort_values('GAME_DATE').reset_index(drop=True)

    return result


if __name__ == "__main__":
    print(get_nba_matchups("05/15/2025", "05/30/2025"))
