import classroom
import scraper


def main():
    print("Checking IOE...")
    scraper.main()

    print("Checking Google Classroom...")
    classroom.check_classroom()


if __name__ == "__main__":
    main()